import { createPinia } from 'pinia'
import { createApp } from 'vue'
import { createRouter, createWebHistory } from 'vue-router'
import { installBook } from '@book/components'
import App from './App.vue'
import { subscribeBookUpdates, useBook } from './store'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', component: () => import('./PageList.vue') },
    { path: '/page/:key', component: () => import('./PageView.vue'), props: (r) => ({ pageKey: r.params.key }) },
    { path: '/render/:key', component: () => import('../render/RenderPage.vue'), props: (r) => ({ pageKey: r.params.key }), meta: { bare: true } },
  ],
})

const app = installBook(createApp(App).use(createPinia()).use(router))
subscribeBookUpdates(useBook())
app.mount('#app')
