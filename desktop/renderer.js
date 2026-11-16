const tabsContainer = document.getElementById('system-tabs')
const message = document.getElementById('message')
const messageTitle = document.getElementById('message-title')
const messageText = document.getElementById('message-text')
const configPathElement = document.getElementById('config-path')
const localIndicator = document.querySelector('.local-indicator')
const themeToggle = document.getElementById('theme-toggle')
const themeToggleLabel = document.getElementById('theme-toggle-label')
const THEME_STORAGE_KEY = 'strategy-a1-theme'

function setTheme (theme, persist = false) {
  const isDark = theme === 'dark'
  const activeTheme = isDark ? 'dark' : 'light'
  const nextThemeLabel = isDark ? 'Tema claro' : 'Tema oscuro'

  document.documentElement.dataset.theme = activeTheme
  themeToggle.setAttribute('aria-pressed', String(isDark))
  themeToggle.setAttribute('aria-label', `Cambiar a ${nextThemeLabel.toLowerCase()}`)
  themeToggle.title = `Cambiar a ${nextThemeLabel.toLowerCase()}`
  themeToggleLabel.textContent = nextThemeLabel

  if (persist) {
    try {
      localStorage.setItem(THEME_STORAGE_KEY, activeTheme)
    } catch {
      // El tema sigue funcionando durante esta sesión si el almacenamiento no está disponible.
    }
  }
}

function loadSavedTheme () {
  try {
    const savedTheme = localStorage.getItem(THEME_STORAGE_KEY)
    return savedTheme === 'dark' ? 'dark' : 'light'
  } catch {
    return 'light'
  }
}

themeToggle.addEventListener('click', () => {
  const nextTheme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'
  setTheme(nextTheme, true)
})
setTheme(loadSavedTheme())

function showMessage (title, text, configPath = '') {
  messageTitle.textContent = title
  messageText.textContent = text
  configPathElement.textContent = configPath
  message.classList.add('visible')
}

async function selectSystem (systemId, buttons) {
  const selected = await window.systems.select(systemId)
  if (!selected) return

  for (const button of buttons) {
    const isSelected = button.dataset.systemId === systemId
    button.setAttribute('aria-selected', String(isSelected))
    button.tabIndex = isSelected ? 0 : -1
  }
}

async function initialize () {
  const configuration = await window.systems.getConfiguration()

  if (configuration.error) {
    showMessage(
      'No se pudo cargar la configuración',
      configuration.error,
      configuration.configPath
    )
    return
  }

  if (configuration.systems.length === 0) {
    showMessage(
      'No hay sistemas configurados',
      'Añade al menos un sistema con su URL en el archivo de configuración.',
      configuration.configPath
    )
    return
  }

  localIndicator.title = `Archivo de configuración: ${configuration.configPath}`

  const buttons = configuration.systems.map((system) => {
    const button = document.createElement('button')
    button.className = 'system-tab'
    button.type = 'button'
    button.setAttribute('role', 'tab')
    button.dataset.systemId = system.id
    button.title = system.url
    button.setAttribute('aria-selected', String(system.id === configuration.selectedId))
    button.tabIndex = system.id === configuration.selectedId ? 0 : -1

    const indicator = document.createElement('span')
    indicator.className = 'tab-indicator'
    indicator.setAttribute('aria-hidden', 'true')
    button.append(indicator, document.createTextNode(system.name))
    button.addEventListener('click', () => selectSystem(system.id, buttons))
    button.addEventListener('keydown', (event) => {
      if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return

      event.preventDefault()
      const currentIndex = buttons.indexOf(button)
      const direction = event.key === 'ArrowRight' ? 1 : -1
      const nextIndex = (currentIndex + direction + buttons.length) % buttons.length
      buttons[nextIndex].focus()
      selectSystem(buttons[nextIndex].dataset.systemId, buttons)
    })
    tabsContainer.append(button)
    return button
  })

  if (configuration.selectedId) {
    await selectSystem(configuration.selectedId, buttons)
  }
}

initialize().catch((error) => {
  showMessage('Error al iniciar', error.message)
})
