import { useStore } from '@nanostores/react'
import { type FC, useCallback, useState } from 'react'

import { MessageAvatar } from '@/components/chat/message-avatar'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { useI18n } from '@/i18n/context'
import {
  $avatarImages,
  $avatarNames,
  type AvatarRole,
  DEFAULT_NAMES,
  setAvatarImage,
  setAvatarName
} from '@/store/avatar'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface AvatarEditorDialogProps {
  open: boolean
  onClose: () => void
}

// ---------------------------------------------------------------------------
// Single participant row
// ---------------------------------------------------------------------------

interface ParticipantRowProps {
  label: string
  placeholder: string
  role: AvatarRole
  onNameChange: (name: string) => void
  savedName: string
}

const ParticipantRow: FC<ParticipantRowProps> = ({ label, onNameChange, placeholder, role, savedName }) => {
  const names = useStore($avatarNames)
  const images = useStore($avatarImages)
  const { t } = useI18n()
  const [localName, setLocalName] = useState(names[role])
  const hasImage = Boolean(images[role])

  return (
    <div className="flex items-center gap-3">
      <MessageAvatar
        editable
        onImageChange={setAvatarImage}
        role={role}
      />
      <div className="min-w-0 flex-1 space-y-1">
        <label className="text-[length:var(--conversation-caption-font-size)] text-(--ui-text-tertiary)">
          {label}
        </label>
        <div className="flex items-center gap-2">
          <Input
            className="h-8 text-[length:var(--conversation-text-font-size)]"
            onChange={event => {
              setLocalName(event.target.value)
              onNameChange(event.target.value)
            }}
            onKeyDown={event => {
              if (event.key === 'Enter') {
                const trimmed = event.currentTarget.value.trim()
                setAvatarName(role, trimmed || savedName)
              }
            }}
            placeholder={placeholder || DEFAULT_NAMES[role]}
            value={localName}
          />
          {hasImage && (
            <Button
              aria-label={t.common.remove}
              onClick={() => setAvatarImage(role, '')}
              size="icon-sm"
              variant="ghost"
            >
              ✕
            </Button>
          )}
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Dialog
// ---------------------------------------------------------------------------

export const AvatarEditorDialog: FC<AvatarEditorDialogProps> = ({ onClose, open }) => {
  const { t } = useI18n()
  const ae = t.assistant.avatarEditor
  const names = useStore($avatarNames)

  // Track pending edits so Done can save all changes at once
  const [pendingNames, setPendingNames] = useState<Partial<Record<AvatarRole, string>>>({})

  const handleNameChange = useCallback((role: AvatarRole, name: string) => {
    setPendingNames(prev => ({ ...prev, [role]: name }))
  }, [])

  const handleDone = useCallback(() => {
    // Flush pending name changes to the store
    for (const role of ['user', 'assistant'] as AvatarRole[]) {
      const pending = pendingNames[role]
      if (pending !== undefined) {
        const trimmed = pending.trim()
        setAvatarName(role, trimmed || names[role] || DEFAULT_NAMES[role])
      }
    }
    onClose()
  }, [onClose, pendingNames, names])

  const handleClose = useCallback(() => {
    setPendingNames({})
    onClose()
  }, [onClose])

  return (
    <Dialog onOpenChange={nextOpen => { if (!nextOpen) onClose(); }} open={open}>
      <DialogContent className="max-w-sm" showCloseButton={false}>
        <DialogHeader>
          <DialogTitle>{ae.title}</DialogTitle>
          <DialogDescription>{ae.description}</DialogDescription>
        </DialogHeader>
        <button
          aria-label={t.common.close}
          className="absolute right-2.5 top-2.5 z-20 flex size-6 items-center justify-center rounded-md text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground"
          onClick={handleClose}
          type="button"
        >
          ✕
        </button>

        <div className="space-y-4 py-2">
          <ParticipantRow
            label={ae.youLabel}
            onNameChange={name => handleNameChange('user', name)}
            placeholder={ae.placeholder}
            role="user"
            savedName={names.user || DEFAULT_NAMES.user}
          />
          <ParticipantRow
            label={ae.hermesLabel}
            onNameChange={name => handleNameChange('assistant', name)}
            placeholder={ae.placeholder}
            role="assistant"
            savedName={names.assistant || DEFAULT_NAMES.assistant}
          />
        </div>

        <DialogFooter>
          <Button onClick={handleDone} variant="secondary">
            {t.common.done}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
