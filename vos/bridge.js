(() => {
  const appId = 'com.ictrek.comfyui'
  const base = '/app/' + appId + '/'
  const start = document.querySelector('#vos-start')
  const status = document.querySelector('#vos-status')
  const guide = document.querySelector('#vos-guide')
  const editor = location.pathname.startsWith(base + 'editor/')
  let locale = navigator.language
  let timer
  let pending
  let generation = 0
  let retry = null

  const words = {
    loading: ['正在启动 ComfyUI…', 'Starting ComfyUI…'],
    noBridge: ['请从 VOS 应用抽屉打开 ComfyUI。', 'Open ComfyUI from the VOS App Drawer.'],
    busy: ['工作区正在启动，或计算资源已占用。请稍后重试。', 'The workspace is starting or the workers are busy. Try again shortly.'],
    failed: ['登录未完成，请重试。', 'Sign-in did not complete. Please try again.'],
    retry: ['重试', 'Try again']
  }
  let message = 'loading'
  const zh = (value) => /^zh(?:[-_]|$)/i.test(value)
  const display = (key = message) => {
    message = key
    if (status) status.textContent = words[key][zh(locale) ? 0 : 1]
    if (retry) {
      retry.textContent = words.retry[zh(locale) ? 0 : 1]
      retry.hidden = key === 'loading' || key === 'noBridge'
    }
    if (guide) {
      document.documentElement.lang = zh(locale) ? 'zh-CN' : 'en'
      for (const panel of guide.querySelectorAll('[data-lang]'))
        panel.hidden = panel.dataset.lang !== (zh(locale) ? 'zh' : 'en')
    }
  }

  const delay = (ms) => new Promise(resolve => setTimeout(resolve, ms))
  const b64 = (value) => btoa(String.fromCharCode(...value)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
  const random = () => b64(crypto.getRandomValues(new Uint8Array(32)))
  const waitBridge = async () => {
    for (let i = 0; i < 80; i++) {
      if (window.vos_platform) return window.vos_platform
      await delay(125)
    }
  }

  const setLocale = async (value) => {
    if (!value) return
    locale = value
    display()
    if (!editor) return
    const nativeLocale = () => zh(locale) ? 'zh' : 'en'
    for (let i = 0; i < 80; i++) {
      const setting = window.comfyAPI?.app?.app?.extensionManager?.setting
      if (setting?.get('Comfy.Locale') !== undefined) {
        const target = nativeLocale()
        if (setting.get('Comfy.Locale') !== target) await setting.set('Comfy.Locale', target)
        return
      }
      await delay(125)
    }
  }

  async function authorize(oauth) {
    const verifier = random()
    const state = random()
    const challenge = b64(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier))))
    const grant = await oauth.authorize({
      client_id: appId, response_type: 'code', scope: 'openid profile email',
      state, nonce: random(), code_challenge: challenge, code_challenge_method: 'S256'
    })
    if (grant.state !== state || !grant.code) throw new Error('VOS authorization state mismatch')
    return oauth.token({ grant_type: 'authorization_code', code: grant.code, code_verifier: verifier, client_id: appId })
  }

  async function signIn(vos) {
    if (pending) return pending
    const epoch = generation
    pending = (async () => {
      display('loading')
      const token = await authorize(vos.api.v1000.oauth2)
      if (epoch !== generation) return
      const session = await fetch(base + 'session', {
        method: 'POST', headers: { Authorization: 'Bearer ' + token.access_token }
      })
      if (!session.ok || epoch !== generation) throw new Error('VOS session rejected')
      const result = await session.json()
      if (editor && result.account_changed) {
        location.replace(base)
        return
      }
      if (start) {
        const prepare = await fetch(base + 'prepare', { method: 'POST' })
        if (prepare.status === 503) {
          display('busy')
          return
        }
        if (!prepare.ok || epoch !== generation) throw new Error('Workspace unavailable')
        const workspace = await prepare.json()
        if (epoch === generation && workspace.editor_path.startsWith(base + 'editor/workspace/'))
          location.replace(workspace.editor_path)
      } else if (editor) {
        clearTimeout(timer)
        timer = setTimeout(() => signIn(vos), Math.max(15, Math.min(1200, (token.expires_in || 300) - 45)) * 1000)
      }
    })().catch(() => {
      if (epoch !== generation) return
      display('failed')
      if (editor) {
        clearTimeout(timer)
        timer = setTimeout(() => signIn(vos), 15000)
      }
    }).finally(() => { pending = null })
    return pending
  }

  ;(async () => {
    if (start) retry = document.querySelector('#vos-retry')
    display()
    const vos = await waitBridge()
    if (!vos) {
      display('noBridge')
      return
    }
    const apply = (value) => setLocale(value).catch(() => console.warn('ComfyUI language sync is unavailable'))
    vos.broadcast?.on('locale-change', event => apply(event.locale))
    const initial = vos.platform?.getLocale?.() ?? vos.getLocale?.() ?? vos.getState?.()
    if (initial) Promise.resolve(initial).then(data => apply(data?.locale)).catch(() => {})
    if (guide) return
    const oauth = vos.api?.v1000?.oauth2
    if (!oauth?.authorize || !oauth?.token) {
      display('noBridge')
      return
    }
    if (retry) retry.onclick = () => signIn(vos)
    vos.broadcast?.on('user-change', async () => {
      generation++
      clearTimeout(timer)
      await Promise.resolve(pending).catch(() => {})
      await fetch(base + 'session', { method: 'DELETE' }).catch(() => {})
      if (editor) location.replace(base)
      else signIn(vos)
    })
    signIn(vos)
  })()
})()
