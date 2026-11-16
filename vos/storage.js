(() => {
  // The pinned editor stores unsaved graphs, clipboard and tab state in browser storage.
  const prefix = 'vos-comfyui:' + document.currentScript.dataset.workspace + ':'
  const scope = (store) => {
    const keys = () => Object.keys(store).filter(key => key.startsWith(prefix)).map(key => key.slice(prefix.length))
    const get = (name) => store.getItem(prefix + name)
    const set = (name, value) => store.setItem(prefix + name, value)
    const remove = (name) => store.removeItem(prefix + name)
    const methods = {
      getItem: get, setItem: set, removeItem: remove, key: index => keys()[index] ?? null,
      clear: () => keys().forEach(remove)
    }
    return new Proxy({}, {
      ownKeys: keys,
      getOwnPropertyDescriptor: (_, key) => get(key) === null ? undefined : {
        configurable: true, enumerable: true, writable: true, value: get(key)
      },
      get: (_, key) => typeof key !== 'string' ? undefined
        : key === 'length' ? keys().length : Object.hasOwn(methods, key) ? methods[key] : get(key) ?? undefined,
      set: (_, key, value) => { set(key, value); return true },
      deleteProperty: (_, key) => { remove(key); return true }
    })
  }
  for (const name of ['localStorage', 'sessionStorage']) {
    try {
      Object.defineProperty(window, name, { value: scope(window[name]) })
    } catch {
      // The WebView may already disable browser storage.
    }
  }
  // Comfy sign-in can restore Firebase sessions from IndexedDB as well.
  try {
    const database = window.indexedDB
    for (const method of ['open', 'deleteDatabase']) {
      const original = database[method].bind(database)
      database[method] = (name, ...args) =>
        original(name === 'firebaseLocalStorageDb' ? prefix + name : name, ...args)
    }
  } catch {
    // IndexedDB can be unavailable in embedded browsers.
  }
  if (window.BroadcastChannel) {
    window.BroadcastChannel = class extends window.BroadcastChannel {
      constructor(name) { super(prefix + name) }
    }
  }
})()
