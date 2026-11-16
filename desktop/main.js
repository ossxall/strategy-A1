const { app, BrowserWindow, WebContentsView, ipcMain } = require('electron/main')
const fs = require('node:fs')
const path = require('node:path')

const HEADER_HEIGHT = 124

app.setName('Strategy A1')

const windowStates = new WeakMap()
let configuration
let configPath
let configurationError = ''

function loadConfiguration () {
  const templatePath = path.join(__dirname, 'config.json')
  configPath = app.isPackaged
    ? path.join(app.getPath('userData'), 'config.json')
    : templatePath

  try {
    if (app.isPackaged) {
      fs.mkdirSync(path.dirname(configPath), { recursive: true })
      if (!fs.existsSync(configPath)) fs.copyFileSync(templatePath, configPath)
    }

    const parsed = JSON.parse(fs.readFileSync(configPath, 'utf8'))
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

    configuration = { systems }
  } catch (error) {
    configuration = { systems: [] }
    configurationError = error.message
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

  try {
    const url = new URL(system.url)
    return (url.protocol === 'http:' || url.protocol === 'https:') && Boolean(url.hostname)
  } catch {
    return false
  }
}

function setViewBounds (state) {
  const { width, height } = state.window.getContentBounds()
  const viewHeight = Math.max(0, height - HEADER_HEIGHT)

  for (const { view } of state.systems.values()) {
    view.setBounds({
      x: 0,
      y: HEADER_HEIGHT,
      width,
      height: viewHeight
    })
  }
}

function createWindow () {
  const win = new BrowserWindow({
    width: 1280,
    height: 820,
    minWidth: 800,
    minHeight: 560,
    autoHideMenuBar: true,
    backgroundColor: '#f3f6fa',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true
    }
  })

  const systems = new Map()
  const state = { window: win, systems, selectedId: null }
  windowStates.set(win, state)

  for (const system of configuration.systems) {
    const view = new WebContentsView({
      webPreferences: {
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true
      }
    })

    view.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
    win.contentView.addChildView(view)
    view.setVisible(false)
    view.webContents.loadURL(system.url).catch(() => {})
    systems.set(system.id, { definition: system, view })
  }

  state.selectedId = configuration.systems[0]?.id ?? null
  if (state.selectedId) systems.get(state.selectedId).view.setVisible(true)

  setViewBounds(state)
  win.on('resize', () => setViewBounds(state))
  win.on('closed', () => {
    windowStates.delete(win)
    for (const { view } of systems.values()) {
      if (!view.webContents.isDestroyed()) view.webContents.close()
    }
  })
  win.loadFile(path.join(__dirname, 'index.html'))
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
