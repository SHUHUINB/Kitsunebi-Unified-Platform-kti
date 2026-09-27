<script setup>
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import ShellLayout from './layouts/ShellLayout.vue'

const route = useRoute()
const bare = computed(() => !!route.meta.public)
</script>

<template>
  <RouterView v-if="bare" />
  <ShellLayout v-else>
    <RouterView />
  </ShellLayout>
</template>
