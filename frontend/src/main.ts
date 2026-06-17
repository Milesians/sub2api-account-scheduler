import { createApp } from 'vue'
import './style.css'
import App from './App.vue'

function initThemeClass() {
  const queryTheme = new URLSearchParams(window.location.search).get('theme')
  const uiMode = new URLSearchParams(window.location.search).get('ui_mode')
  const embeddedTheme = queryTheme === 'dark' || queryTheme === 'light' ? queryTheme : null
  const savedTheme = localStorage.getItem('theme')
  const shouldUseDark =
    embeddedTheme === 'dark' ||
    (!embeddedTheme &&
      (savedTheme === 'dark' ||
        (!savedTheme && window.matchMedia('(prefers-color-scheme: dark)').matches)))
  document.documentElement.classList.toggle('dark', shouldUseDark)
  document.documentElement.classList.toggle('embedded', uiMode === 'embedded')
  document.documentElement.style.colorScheme = shouldUseDark ? 'dark' : 'light'
}

initThemeClass()
createApp(App).mount('#app')
