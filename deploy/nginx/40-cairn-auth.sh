#!/bin/sh
# Renders /etc/nginx/.htpasswd and /etc/nginx/conf.d/default.conf from the
# `web.auth` section of dispatch.yaml (mounted read-only at /cairn/dispatch.yaml).
#
# Expected schema (values may be quoted; - name must come before password):
#
#   web:
#     auth:
#       realm: "Cairn"
#       users:
#         - name: admin
#           password: "plaintext"
#
# `CAIRN_UI_TOKEN` (optional, env) is rendered into `X-Cairn-UI-Token` for every
# proxied request; the server requires it on endpoints that could weaken the guard.
#
# Plaintext passwords are SHA-512 crypt-hashed with the base image's built-in
# busybox mkpasswd at container start; only the hash reaches .htpasswd. If
# `web.auth` is absent the proxy
# starts WITHOUT authentication (warning). Any malformed config aborts
# container startup (the stock nginx entrypoint runs with `set -e`).
set -eu

CONFIG="${CAIRN_DISPATCH_CONFIG:-/cairn/dispatch.yaml}"
TEMPLATE=/etc/cairn/default.conf.template
CONF=/etc/nginx/conf.d/default.conf
HTPASSWD=/etc/nginx/.htpasswd

if [ ! -r "$CONFIG" ]; then
    echo "cairn-auth: cannot read $CONFIG (mount dispatch.yaml read-only at /cairn/dispatch.yaml)" >&2
    exit 1
fi

tmp_realm=$(mktemp)
tmp_users=$(mktemp)
trap 'rm -f "$tmp_realm" "$tmp_users"' EXIT

# Strict line-oriented reader for the schema above. Writes "name<TAB>password"
# lines to $tmp_users and the realm (possibly empty) to $tmp_realm.
awk -v users_file="$tmp_users" -v realm_file="$tmp_realm" '
function trim(s) { sub(/^[ \t]+/, "", s); sub(/[ \t]+$/, "", s); return s }
function unquote(s, q, end) {
    s = trim(s)
    q = substr(s, 1, 1)
    if (q == "\"" || q == "\x27") {
        end = index(substr(s, 2), q)
        if (end == 0) fail("unterminated quote in value: " s)
        return substr(s, 2, end - 1)
    }
    sub(/[ \t]+#.*/, "", s)   # strip inline comment on unquoted values
    return trim(s)
}
function fail(msg) {
    print "cairn-auth: dispatch.yaml: " msg > "/dev/stderr"
    failed = 1
    exit 1
}
function flush_user() {
    if (name == "") return
    if (password == "") fail("user \"" name "\" has no password")
    if (name ~ /^-/) fail("user name must not start with \"-\": " name)
    if (name ~ /[:\t]/) fail("user name must not contain \":\" or tabs: " name)
    if (password ~ /\t/) fail("user \"" name "\": password must not contain tabs")
    print name "\t" password > users_file
    count++
    name = ""
    password = ""
}
BEGIN { state = 0; count = 0 }   # 0 outside web, 1 in web, 2 in auth, 3 in users
{
    line = $0
    if (line ~ /^[ \t]*#/ || line ~ /^[ \t]*$/) next
    n = match(line, /[^ \t]/) - 1
    text = substr(line, n + 1)

    if (state == 0) {
        if (n == 0 && text ~ /^web:/) state = 1
        next
    }
    if (state == 1) {
        if (n == 0) { state = (text ~ /^web:/ ? 1 : 0); next }
        if (text ~ /^auth:/) state = 2
        next
    }
    if (state == 2) {
        if (n == 0) { flush_user(); state = 0; next }
        if (text ~ /^realm:/) {
            realm = unquote(substr(text, index(text, ":") + 1))
            if (realm == "") fail("web.auth.realm is empty")
            if (realm !~ /^[A-Za-z0-9 _.,:()\/-]+$/) fail("web.auth.realm contains unsupported characters: " realm)
        } else if (text ~ /^users:/) {
            state = 3
        }
        next
    }
    if (state == 3) {
        if (n == 0) { flush_user(); state = 0; next }
        if (text ~ /^- name:/) {
            flush_user()
            name = unquote(substr(text, index(text, "name:") + 5))
            if (name == "") fail("users entry has empty name")
        } else if (text ~ /^-/) {
            fail("unsupported entry in web.auth.users (expected \"- name: ...\"): " text)
        } else if (text ~ /^password:/) {
            if (name == "") fail("password entry without a preceding \"- name\"")
            if (password != "") fail("user \"" name "\" has two password entries")
            password = unquote(substr(text, index(text, ":") + 1))
        } else if (text ~ /^name:/) {
            fail("user entries must start with \"- name: \", got: " text)
        } else {
            fail("unsupported key in web.auth.users: " text)
        }
        next
    }
}
END {
    if (failed) exit 1
    if (state == 3) flush_user()
    if (count > 0 && realm == "") realm = "Cairn"
    print realm > realm_file
}
' "$CONFIG"

REALM=$(cat "$tmp_realm")

# Optional shared secret for the UI channel. When set, the server requires it on the
# endpoints that can weaken the guard (write rules, decide approvals, edit scope), and
# this proxy injects it for browser traffic. The worker channel never sees it.
UI_TOKEN="${CAIRN_UI_TOKEN:-}"
if [ -n "$UI_TOKEN" ]; then
    case "$UI_TOKEN" in
        *[!A-Za-z0-9_-]*)
            echo "cairn-auth: CAIRN_UI_TOKEN must match [A-Za-z0-9_-]+" >&2
            exit 1
            ;;
    esac
fi

if [ -s "$tmp_users" ]; then
    : > "$HTPASSWD"
    TAB=$(printf '\t')
    while IFS="$TAB" read -r name password; do
        hash=$(printf '%s\n' "$password" | mkpasswd -P 0 -m sha512)
        case "$hash" in
            '$6$'*) printf '%s:%s\n' "$name" "$hash" >> "$HTPASSWD" ;;
            *)
                echo "cairn-auth: mkpasswd failed for user '$name'" >&2
                exit 1
                ;;
        esac
    done < "$tmp_users"
    awk -v realm="$REALM" -v ui_token="$UI_TOKEN" '{ gsub(/__CAIRN_REALM__/, realm); gsub(/__CAIRN_UI_TOKEN__/, ui_token); print }' "$TEMPLATE" > "$CONF"
    chgrp nginx "$HTPASSWD" || true
    chmod 640 "$HTPASSWD"
    echo "cairn-auth: Basic Auth enabled (realm \"$REALM\", $(wc -l < "$HTPASSWD") user(s))"
else
    rm -f "$HTPASSWD"
    awk -v ui_token="$UI_TOKEN" '!/auth_basic/ { gsub(/__CAIRN_UI_TOKEN__/, ui_token); print }' "$TEMPLATE" > "$CONF"
    echo "cairn-auth: WARNING: no web.auth section in dispatch.yaml — starting WITHOUT authentication" >&2
fi
