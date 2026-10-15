<script setup lang="ts">
import { computed } from 'vue'
import JobCard from './JobCard.vue'
import { jobs, refreshJobs } from './jobs'

const props = defineProps<{ id: string }>()
const emit = defineEmits<{ back: []; open: [id: string] }>()

const job = computed(() => jobs.list.find((j) => j.id === props.id))

async function opened(id: string) {
  await refreshJobs()
  emit('open', id)
}
async function removed() {
  await refreshJobs()
  emit('back')
}
</script>

<template>
  <div class="stack">
    <a class="back small" href="#conversions" @click.prevent="emit('back')">← All conversions</a>
    <JobCard v-if="job" :key="job.id" :job="job" detailed @changed="refreshJobs" @removed="removed" @created="opened" />
    <div v-else-if="!jobs.loaded" class="row muted"><span class="spinner" /> Loading…</div>
    <div v-else class="card stack">
      <h2>Conversion not found</h2>
      <p class="muted">It may have been removed.</p>
    </div>
  </div>
</template>

<style scoped>
.back { color: var(--muted); text-decoration: none; justify-self: start; }
.back:hover { color: var(--ink); }
</style>
