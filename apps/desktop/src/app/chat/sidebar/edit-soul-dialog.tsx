import { useEffect, useState } from 'react'

import { CodeEditor } from '@/components/chat/code-editor'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { getProfileSoul, updateProfileSoul } from '@/hermes'
import { useI18n } from '@/i18n'
import { notify, notifyError } from '@/store/notifications'

export function EditSoulDialog({ onClose, profileName }: { onClose: () => void; profileName: string }) {
  const { t } = useI18n()
  const p = t.profiles
  const [content, setContent] = useState('')
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setContent('')
    getProfileSoul(profileName)
      .then(soul => !cancelled && setContent(soul.content))
      .catch(err => !cancelled && notifyError(err, p.failedLoadSoul))
      .finally(() => !cancelled && setLoading(false))

    return () => void (cancelled = true)
  }, [p, profileName])

  const save = async () => {
    setSaving(true)

    try {
      await updateProfileSoul(profileName, content)
      notify({ kind: 'success', title: p.soulSaved, message: profileName })
      onClose()
    } catch (err) {
      notifyError(err, p.failedSaveSoul)
    } finally {
      setSaving(false)
    }
  }

  return (
    <Dialog onOpenChange={open => !open && !saving && onClose()} open>
      <DialogContent className="max-w-2xl">
        <DialogHeader><DialogTitle>{profileName} · SOUL.md</DialogTitle></DialogHeader>
        <div className="h-80">
          {!loading && <CodeEditor filePath="SOUL.md" framed initialValue={content} onCancel={() => !saving && onClose()} onChange={setContent} onSave={() => void save()} />}
        </div>
        <DialogFooter>
          <Button disabled={saving} onClick={onClose} type="button" variant="ghost">{t.common.cancel}</Button>
          <Button disabled={saving || loading} onClick={() => void save()}>{saving ? p.saving : p.saveSoul}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
