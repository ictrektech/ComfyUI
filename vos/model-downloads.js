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

  async function choose(models) {
    show(label('正在核对模型来源…', 'Checking model sources…'))
    const { choices } = await request(endpoint + '/sources', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ models })
    })
    if (choices.some(options => !options.length)) throw new Error('400')
    if (!choices.some(options => options.length > 1)) return models

    const dialog = document.createElement('dialog')
    dialog.dataset.testid = 'vos-model-download-sources'
    dialog.setAttribute('aria-label', label('选择模型下载来源', 'Choose model download sources'))
    Object.assign(dialog.style, {
      colorScheme: 'dark', background: '#24272e', color: '#fff', border: '1px solid #484e5b',
      borderRadius: '12px', padding: '20px', width: 'min(570px, 90vw)', maxHeight: '80vh'
    })
    const form = document.createElement('form')
    form.method = 'dialog'
    const title = document.createElement('h2')
    title.textContent = dialog.getAttribute('aria-label')
    Object.assign(title.style, { fontSize: '17px', fontWeight: '600', marginBottom: '12px' })
    form.append(title)
    const controls = models.map((model, index) => {
      const row = document.createElement('label')
      Object.assign(row.style, {
        display: 'flex', justifyContent: 'space-between', alignItems: 'center',
        gap: '12px', margin: '10px 0', overflowWrap: 'anywhere'
      })
      const name = document.createElement('span')
      name.textContent = model.name
      row.append(name)
      form.append(row)
      if (choices[index].length < 2) return null
      const select = document.createElement('select')
      Object.assign(select.style, { padding: '6px', border: '1px solid #5b626e', borderRadius: '6px' })
      for (const [value, caption] of [['hf', 'Hugging Face'], ['ms', 'ModelScope']]) {
        select.add(new Option(caption, value))
      }
      select.value = new URL(model.url).hostname === 'huggingface.co' ? 'hf' : 'ms'
      row.append(select)
      return select
    })
    const actions = document.createElement('div')
    Object.assign(actions.style, { display: 'flex', justifyContent: 'flex-end', gap: '10px', marginTop: '18px' })
    for (const [value, text] of [['cancel', label('取消', 'Cancel')], ['download', label('开始下载', 'Start download')]]) {
      const button = document.createElement('button')
      button.value = value
      button.textContent = text
      Object.assign(button.style, {
        padding: '7px 11px', borderRadius: '6px', background: value === 'download' ? '#196ac8' : '#414753'
      })
      actions.append(button)
    }
    form.append(actions)
    dialog.append(form)
    document.body.append(dialog)
    return new Promise(resolve => {
      dialog.addEventListener('close', () => {
        const selected = dialog.returnValue === 'download'
          ? models.map((model, index) => controls[index] ? { ...model, source: controls[index].value } : model)
          : []
        dialog.remove()
        resolve(selected)
      }, { once: true })
      dialog.showModal()
    })
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
    const pending = [...waiting.values()]
    waiting.clear()
    try {
      const models = await choose(pending)
      if (!models.length) return
      show(label('正在提交到 V-Scheduler…', 'Submitting to V-Scheduler…'))
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
    if (!/^https:\/\/(huggingface\.co|(?:www\.)?modelscope\.cn)\//.test(url)) {
      show(label('请选择 Hugging Face 或 ModelScope 的仓库文件链接。',
        'Use a Hugging Face or ModelScope repository link.'))
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
