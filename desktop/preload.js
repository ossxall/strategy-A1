const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('systems', {
  getConfiguration: () => ipcRenderer.invoke('systems:get-configuration'),
  select: (systemId) => ipcRenderer.invoke('systems:select', systemId),
  reloadCurrent: () => ipcRenderer.invoke('systems:reload-current')
})
