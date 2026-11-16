(() => {
  const endpoint = '/app/com.ictrek.comfyui/model-downloads'
  const storageKey = 'vosModelDownloads'
  const waiting = new Map()
  let timer
  let notice
  let activeBatches = []

  const app = () => window.comfyAPI?.app?.app
  const chinese = () => (app()?.extensionManager?.setting?.get('Comfy.Locale') ?? navigator.language).startsWith('zh')
  const label = (zh, en) => chinese() ? zh : en
  const delay = ms => new Promise(resolve => setTimeout(resolve, ms))

  function show(message) {
    if (!notice) {
      notice = document.createElement('output')
      notice.setAttribute('aria-live', 'polite')
      notice.dataset.testid = 'vos-model-download-status'
      Object.assign(notice.style, {
        position: 'fixed', right: '16px', bottom: '20px', zIndex: '1500',
        maxWidth: 'min(420px, 85vw)', borderRadius: '8px', padding: '10px 14px',
        color: '#fff', background: '#252830', boxShadow: '0 2px 12px #0006',
        fontSize: '13px', pointerEvents: 'none'
      })
      document.body.appendChild(notice)
    }
    notice.textContent = message
  }

  async function request(url, options) {
    const result = await fetch(url, options)
    if (!result.ok) throw new Error(String(result.status))
    return result.json()
  }

  function saveBatches() {
    try {
      sessionStorage.setItem(storageKey, JSON.stringify(activeBatches))
    } catch {
      // The download still runs in Model Hub if the browser blocks storage.
    }
  }

  async function watch(id) {
    let lastReady = -1
    let failedChecks = 0
    let waitingForEditor = true
    while (waitingForEditor && failedChecks < 80) {
      waitingForEditor = !app()?.rootGraphOrUndefined
      if (waitingForEditor) {
        failedChecks++
        await delay(250)
      }
    }
    failedChecks = 0
    while (true) {
      try {
        const { models } = await request(endpoint + '/' + id)
        const ready = models.filter(model => model.status === 'ready')
        const failed = models.filter(model => model.status === 'failed')
        if (ready.length > lastReady && ready.length) await app().refreshMissingModels({ silent: true })
        lastReady = ready.length
        if (ready.length + failed.length === models.length) {
          activeBatches = activeBatches.filter(batch => batch !== id)
          saveBatches()
          if (failed.length) {
            show(label('部分或全部模型未就绪。请在 V-Scheduler 查看任务并重试：',
              'Some models are unavailable. Check the V-Scheduler tasks and retry: ') +
              failed.map(model => model.name).join(', '))
          } else {
            show(label('模型已就绪，已刷新工作流。', 'Models are ready and the workflow has been refreshed.'))
          }
          return
        }
        show(label(`V-Scheduler 正在下载模型（${ready.length}/${models.length} 就绪）`,
          `Downloading in V-Scheduler (${ready.length}/${models.length} ready)`))
        failedChecks = 0
      } catch (error) {
        if (error.message === '404') {
          activeBatches = activeBatches.filter(batch => batch !== id)
          saveBatches()
          return
        }
        if (++failedChecks === 5) {
          show(label('无法查询下载进度。请检查 V-Scheduler 后重新打开编辑器。',
            'Download status is unavailable. Check V-Scheduler and reopen the editor.'))
          return
        }
      }
      await delay(3000)
    }
  }

  async function submit() {
    const models = [...waiting.values()]
    waiting.clear()
    show(label('正在提交到 V-Scheduler…', 'Submitting to V-Scheduler…'))
    try {
      const { id } = await request(endpoint, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ models })
      })
      activeBatches.push(id)
      saveBatches()
      await watch(id)
    } catch (error) {
      const message = error.message === '409'
        ? label('该工作流模型路径已绑定到另一份权重。', 'This workflow model path is bound to a different weight.')
        : label('无法提交。请检查 V-Scheduler 及模型链接，然后重试。',
          'Could not start the download. Check V-Scheduler and the model links, then retry.')
      show(message)
    }
  }

  window.vosComfyModelDownload = model => {
    const { url, name, directory } = model
    if (!/^https:\/\/(huggingface\.co|(?:www\.)?modelscope\.cn)\//.test(url) || !/\.(safetensors|sft)$/.test(name)) {
      show(label('V-Scheduler 支持 Hugging Face 和 ModelScope 的 safetensors 权重。',
        'V-Scheduler downloads safetensors weights from Hugging Face and ModelScope.'))
      return
    }
    waiting.set(JSON.stringify([url, name, directory]), { url, name, directory })
    clearTimeout(timer)
    timer = setTimeout(submit, 0)
  }

  window.addEventListener('DOMContentLoaded', () => {
    try {
      activeBatches = JSON.parse(sessionStorage.getItem(storageKey) ?? '[]').filter(id => /^[a-f0-9]{32}$/.test(id))
      for (const id of activeBatches) void watch(id)
    } catch {
      // Ignore stale or unavailable browser state.
    }
  })
})()
