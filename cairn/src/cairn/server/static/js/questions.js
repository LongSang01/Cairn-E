// Side-question stream and actions.

export const questionsMixins = {
    openQuestionsTab() {
      this.sideTab = 'questions';
      this.loadQuestions();
      if (this.hasActiveQuestion()) this.startQuestionStream();
    },

    hasActiveQuestion() {
      return this.questions.some(q => q.status === 'pending' || q.status === 'running');
    },

    async loadQuestions() {
      if (!this.selectedProjectId) return;
      try {
        this.questions = await this.api('GET', `/projects/${this.selectedProjectId}/questions`);
        if (this.hasActiveQuestion()) this.startQuestionStream();
      } catch (e) {
        // keep the previous list; the next open retries
      }
    },

    startQuestionStream() {
      if (!this.selectedProjectId || this.questionStream) return;
      const source = new EventSource(`/projects/${this.selectedProjectId}/questions/stream`);
      this.questionStream = source;
      const apply = payload => {
        this.questions = payload.questions || [];
        this.$nextTick(() => {
          const list = this.$refs.questionList;
          if (list && list.scrollHeight - list.scrollTop - list.clientHeight < 120) {
            list.scrollTop = list.scrollHeight;
          }
        });
      };
      source.addEventListener('snapshot', event => apply(JSON.parse(event.data)));
      source.addEventListener('questions', event => apply(JSON.parse(event.data)));
      source.addEventListener('end', event => {
        apply(JSON.parse(event.data));
        this.stopQuestionStream();
      });
      source.addEventListener('gone', () => this.stopQuestionStream());
      source.onerror = () => {
        if (source.readyState === EventSource.CLOSED) this.stopQuestionStream();
      };
    },

    stopQuestionStream() {
      if (!this.questionStream) return;
      this.questionStream.close();
      this.questionStream = null;
    },

    resetQuestionState() {
      this.stopQuestionStream();
      this.questions = [];
      this.questionInput = '';
    },

    async askQuestion() {
      const content = this.questionInput.trim();
      if (!content || this.questionSending || !this.selectedProjectId) return;
      this.questionSending = true;
      try {
        const question = await this.api('POST', `/projects/${this.selectedProjectId}/questions`, { content });
        this.questions = [...this.questions, question];
        this.questionInput = '';
        this.startQuestionStream();
      } catch (e) {
        this.showToast(e.message, 'error');
      } finally {
        this.questionSending = false;
      }
    },

    async cancelQuestion(questionId) {
      if (!this.selectedProjectId) return;
      try {
        await this.api('POST', `/projects/${this.selectedProjectId}/questions/${questionId}/cancel`);
        await this.loadQuestions();
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },

    async clearQuestions() {
      if (!this.selectedProjectId) return;
      try {
        await this.api('DELETE', `/projects/${this.selectedProjectId}/questions`);
        await this.loadQuestions();
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },

    questionStatusLabel(status) {
      const labels = {
        pending: '等待中',
        running: '回答中',
        answered: '已回答',
        failed: '失败',
        cancelled: '已取消',
      };
      return labels[status] || status;
    },

    questionStatusDotClass(status) {
      if (status === 'pending') return 'bg-slate-300';
      if (status === 'running') return 'bg-sky-400 animate-pulse';
      if (status === 'answered') return 'bg-emerald-400';
      if (status === 'cancelled') return 'bg-slate-300';
      return 'bg-rose-400';
    },
};
