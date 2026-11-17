const { app, BrowserWindow, WebContentsView, ipcMain } = require('electron/main')
const fs = require('node:fs')
const path = require('node:path')

const HEADER_HEIGHT = 100
const PERSISTENT_PARTITION = 'persist:strategy-a1'
const DEV_TOOLS_ENABLED = !app.isPackaged

app.setName('Strategy A1')

const windowStates = new WeakMap()
let configuration
let configPath
let configurationError = ''

function loadConfiguration () {
  const templatePath = path.join(__dirname, 'config.json')
  configPath = getConfigurationPath(templatePath)

  try {
    if (app.isPackaged) {
      fs.mkdirSync(path.dirname(configPath), { recursive: true })
      if (!fs.existsSync(configPath)) fs.copyFileSync(templatePath, configPath)
    }

    const parsed = JSON.parse(fs.readFileSync(configPath, 'utf8'))
    if (app.isPackaged) {
      const template = JSON.parse(fs.readFileSync(templatePath, 'utf8'))
      let migrated = false

      if (!Object.hasOwn(parsed, 'authUrl')) {
        if (isValidWebUrl(template.authUrl)) {
          parsed.authUrl = template.authUrl.trim()
          migrated = true
        }
      }

      const previousTerminalUrl = 'http://127.0.0.1:8888'
      const terminal = Array.isArray(parsed.systems)
        ? parsed.systems.find((system) => system?.id === 'terminal')
        : null
      const configuredTerminal = Array.isArray(template.systems)
        ? template.systems.find((system) => system?.id === 'terminal')
        : null

      if (
        terminal?.url === previousTerminalUrl &&
        isValidWebUrl(configuredTerminal?.url)
      ) {
        terminal.url = configuredTerminal.url.trim()
        migrated = true
      }

      if (migrated) {
        fs.writeFileSync(configPath, `${JSON.stringify(parsed, null, 2)}\n`)
      }
    }

    const authUrl = typeof parsed.authUrl === 'string' ? parsed.authUrl.trim() : ''
    if (!isValidWebUrl(authUrl)) {
      throw new Error('La configuración debe incluir una authUrl HTTP o HTTPS válida.')
    }

    const configuredSystems = Array.isArray(parsed.systems)
      ? parsed.systems
          .filter(isValidSystem)
          .map((system) => ({
            id: system.id.trim(),
            name: system.name.trim(),
            url: system.url.trim()
          }))
      : []
    const seenIds = new Set()
    const systems = configuredSystems.filter((system) => {
      if (seenIds.has(system.id)) return false
      seenIds.add(system.id)
      return true
    })

    if (systems.length === 0) {
      throw new Error('La configuración debe incluir al menos un sistema con id, name y URL HTTP o HTTPS.')
    }

    configuration = { authUrl, systems }
  } catch (error) {
    configuration = { authUrl: '', systems: [] }
    configurationError = error.message
  }
}

function getConfigurationPath (templatePath) {
  if (!app.isPackaged) return templatePath

  const binaryPath = process.env.APPIMAGE || app.getPath('exe')
  const adjacentConfigPath = path.join(path.dirname(binaryPath), 'config.json')

  if (fs.existsSync(adjacentConfigPath)) return adjacentConfigPath
  return path.join(app.getPath('userData'), 'config.json')
}

function isValidWebUrl (value) {
  if (typeof value !== 'string' || !value.trim()) return false

  try {
    const url = new URL(value)
    return (url.protocol === 'http:' || url.protocol === 'https:') && Boolean(url.hostname)
  } catch {
    return false
  }
}

function isValidSystem (system) {
  if (
    !system ||
    typeof system.id !== 'string' ||
    typeof system.name !== 'string' ||
    typeof system.url !== 'string' ||
    !system.id.trim() ||
    !system.name.trim()
  ) {
    return false
  }

  return isValidWebUrl(system.url)
}

function setViewBounds (state) {
  const { width, height } = state.window.getContentBounds()

  if (!state.authenticated) {
    state.authView?.setBounds({ x: 0, y: 0, width, height })
    return
  }

  for (const { view } of state.systems.values()) {
    view.setBounds({
      x: 0,
      y: HEADER_HEIGHT,
      width,
      height: Math.max(0, height - HEADER_HEIGHT)
    })
  }
}

function isSuccessfulAuthRedirect (url, authUrl) {
  try {
    const redirectedUrl = new URL(url)
    const authPageUrl = new URL(authUrl)
    const authHost = authPageUrl.hostname.toLowerCase().replace(/^www\./, '')
    const redirectedHost = redirectedUrl.hostname.toLowerCase().replace(/^www\./, '')
    const isSameSite = redirectedHost === authHost ||
      redirectedHost.endsWith(`.${authHost}`)
    const pathname = redirectedUrl.pathname.toLowerCase().replace(/\/+$/, '') || '/'
    const authPathname = authPageUrl.pathname.toLowerCase().replace(/\/+$/, '') || '/'

    return redirectedUrl.protocol === authPageUrl.protocol &&
      isSameSite &&
      pathname !== authPathname
  } catch {
    return false
  }
}

function showApplication (state) {
  if (state.authenticated || state.window.isDestroyed()) return

  state.authenticated = true
  state.window.contentView.removeChildView(state.authView)
  state.authView.webContents.close()
  state.authView = null

  for (const system of configuration.systems) {
    const view = new WebContentsView({
      webPreferences: {
        partition: PERSISTENT_PARTITION,
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
        devTools: DEV_TOOLS_ENABLED
      }
    })

    view.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
    state.window.contentView.addChildView(view)
    view.setVisible(false)
    view.webContents.loadURL(system.url).catch(() => {})
    state.systems.set(system.id, { definition: system, view })
  }

  state.selectedId = configuration.systems[0]?.id ?? null
  if (state.selectedId) state.systems.get(state.selectedId).view.setVisible(true)

  setViewBounds(state)
  state.window.loadFile(path.join(__dirname, 'index.html'))
}

function createWindow () {
  const win = new BrowserWindow({
    width: 1280,
    height: 820,
    minWidth: 800,
    minHeight: 560,
    autoHideMenuBar: true,
    backgroundColor: '#f3f6fa',
    show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      devTools: DEV_TOOLS_ENABLED
    }
  })

  const systems = new Map()
  const state = {
    window: win,
    systems,
    selectedId: null,
    authenticated: false,
    authView: null
  }
  windowStates.set(win, state)

  const authView = new WebContentsView({
    webPreferences: {
      partition: PERSISTENT_PARTITION,
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      devTools: DEV_TOOLS_ENABLED
    }
  })
  state.authView = authView

  authView.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
  const onAuthNavigation = (_event, url) => {
    if (isSuccessfulAuthRedirect(url, configuration.authUrl)) showApplication(state)
  }
  authView.webContents.on('did-navigate', onAuthNavigation)
  authView.webContents.on('did-navigate-in-page', onAuthNavigation)

  win.contentView.addChildView(authView)

  setViewBounds(state)
  win.on('resize', () => setViewBounds(state))
  win.on('closed', () => {
    windowStates.delete(win)
    if (state.authView && !state.authView.webContents.isDestroyed()) {
      state.authView.webContents.close()
    }
    for (const { view } of systems.values()) {
      if (!view.webContents.isDestroyed()) view.webContents.close()
    }
  })

  win.show()
  if (configuration.authUrl) {
    authView.webContents.loadURL(configuration.authUrl).catch(() => {})
  }
}

ipcMain.handle('systems:get-configuration', (event) => {
  const win = BrowserWindow.fromWebContents(event.sender)
  const state = win && windowStates.get(win)

  return {
    systems: configuration.systems,
    selectedId: state?.selectedId ?? null,
    configPath,
    error: configurationError
  }
})

ipcMain.handle('systems:select', (event, systemId) => {
  const win = BrowserWindow.fromWebContents(event.sender)
  const state = win && windowStates.get(win)
  const selected = state?.systems.get(systemId)

  if (!state || !selected) return false

  for (const [id, { view }] of state.systems) {
    view.setVisible(id === systemId)
  }

  state.selectedId = systemId
  setViewBounds(state)
  return true
})

ipcMain.handle('systems:reload-current', (event) => {
  const win = BrowserWindow.fromWebContents(event.sender)
  const state = win && windowStates.get(win)
  const selected = state?.systems.get(state.selectedId)

  if (!selected || selected.view.webContents.isDestroyed()) return false

  selected.view.webContents.reload()
  return true
})

app.whenReady().then(() => {
  loadConfiguration()
  createWindow()

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow()
  })
})

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit()
})
