FROM nginx:alpine

# Password hashing uses the base image's built-in busybox mkpasswd (SHA-512
# crypt) — no extra packages.

COPY default.conf.template /etc/cairn/default.conf.template
COPY 40-cairn-auth.sh /docker-entrypoint.d/40-cairn-auth.sh
RUN chmod +x /docker-entrypoint.d/40-cairn-auth.sh
