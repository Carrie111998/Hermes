import { useStore } from '@nanostores/react'
import type * as React from 'react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { CodeEditor } from '@/components/chat/code-editor'
import { PageLoader } from '@/components/page-loader'
import { Button } from '@/components/ui/button'
import { ProfileGlyph } from '@/components/ui/profile-glyph'
import {
  getProfileSoul,
  removeProfileAvatar,
  type ProfileInfo,
  updateProfileSoul,
  uploadProfileAvatar
} from '@/hermes'
import { useI18n } from '@/i18n'
import { displayPath } from '@/lib/display-path'
import { AlertTriangle, Save } from '@/lib/icons'
import { resolveProfileColor } from '@/lib/profile-color'
import { normalize } from '@/lib/text'
import { notify, notifyError } from '@/store/notifications'
import { $profileColors, refreshProfiles } from '@/store/profile'
import { $connection } from '@/store/session'

import { useRefreshHotkey } from '../hooks/use-refresh-hotkey'
import {
  Panel,
  PanelAddButton,
  PanelBody,
  PanelDetail,
  PanelEmpty,
  PanelHeader,
  PanelList,
  PanelListRow,
  type PanelMenuItem,
  PanelMeta,
  PanelPill,
  PanelSectionLabel
} from '../overlays/panel'

import { CreateProfileDialog } from './create-profile-dialog'
import { DeleteProfileDialog } from './delete-profile-dialog'
import { RenameProfileDialog } from './rename-profile-dialog'

interface ProfilesViewProps {
  onClose: () => void
}

export function ProfilesView({ onClose }: ProfilesViewProps) {
  const { t } = useI18n()
  const p = t.profiles
  const [profiles, setProfiles] = useState<null | ProfileInfo[]>(null)
  const [selectedName, setSelectedName] = useState<null | string>(null)
  const [query, setQuery] = useState('')
  const [createOpen, setCreateOpen] = useState(false)
  const [pendingRename, setPendingRename] = useState<null | ProfileInfo>(null)
  const [pendingDelete, setPendingDelete] = useState<null | ProfileInfo>(null)
  // Bumped after every avatar upload/remove so every <img> using the avatar
  // URL gets a fresh `&v=` token — the backend URL path is stable, so
  // without this the browser serves the cached (stale) image.
  const [avatarVersion, setAvatarVersion] = useState(0)

  const refresh = useCallback(async () => {
    try {
      const list = await refreshProfiles()
      setProfiles(list)
      setSelectedName(current => {
        if (current && list.some(p => p.name === current)) {
          return current
        }

        return list.find(p => p.is_default)?.name ?? list[0]?.name ?? null
      })
    } catch (err) {
      notifyError(err, p.failedLoad)
    }
  }, [p])

  useRefreshHotkey(refresh)

  useEffect(() => {
    void refresh()
  }, [refresh])

  const selected = useMemo(() => {
    if (!profiles) {
      return null
    }

    return profiles.find(p => p.name === selectedName) ?? profiles[0] ?? null
  }, [profiles, selectedName])

  const visibleProfiles = useMemo(() => {
    const q = normalize(query)

    if (!profiles || !q) {
      return profiles ?? []
    }

    return profiles.filter(
      profile => profile.name.toLowerCase().includes(q) || (profile.model ?? '').toLowerCase().includes(q)
    )
  }, [profiles, query])

  // The shared Create/Rename dialogs own the createProfile / renameProfile /
  // updateProfileSoul calls; the panel just selects the resulting profile and
  // re-pulls the list.
  const selectAndRefresh = useCallback(
    async (name: string) => {
      setSelectedName(name)
      await refresh()
    },
    [refresh]
  )

  // Called from ProfileDetail after a successful avatar upload or remove.
  // Bumps the version (cache-busting) AND re-pulls the profiles list so the
  // avatar_url field (which may flip to/from null) is also up to date.
  const onAvatarChanged = useCallback(async () => {
    setAvatarVersion(v => v + 1)
    await refresh()
  }, [refresh])

  return (
    <Panel closeLabel={p.close} onClose={onClose}>
      {!profiles ? (
        <PageLoader label={p.loading} />
      ) : profiles.length === 0 ? (
        <PanelEmpty
          action={
            <Button onClick={() => setCreateOpen(true)} size="sm">
              {p.newProfile}
            </Button>
          }
          description={p.createDesc}
          icon="organization"
          title={p.noProfiles}
        />
      ) : (
        <>
          <PanelHeader subtitle={p.count(profiles.length)} title={p.title} />
          <PanelBody>
            <PanelList
              onSearchChange={setQuery}
              searchLabel={p.search}
              searchPlaceholder={p.search}
              searchValue={query}
            >
              {visibleProfiles.map(profile => (
                <ProfileRow
                  active={selected?.name === profile.name}
                  avatarVersion={avatarVersion}
                  key={profile.name}
                  menuItems={
                    profile.is_default
                      ? []
                      : [
                          { icon: 'edit', label: p.renameMenu, onSelect: () => setPendingRename(profile) },
                          {
                            icon: 'trash',
                            label: t.common.delete,
                            onSelect: () => setPendingDelete(profile),
                            tone: 'danger'
                          }
                        ]
                  }
                  onSelect={() => setSelectedName(profile.name)}
                  profile={profile}
                />
              ))}
              <PanelAddButton label={p.newProfile} onClick={() => setCreateOpen(true)} />
            </PanelList>

            {selected ? (
              <ProfileDetail
                avatarVersion={avatarVersion}
                key={selected.name}
                onAvatarChanged={onAvatarChanged}
                profile={selected}
              />
            ) : (
              <PanelEmpty description={p.selectPrompt} icon="account" />
            )}
          </PanelBody>
        </>
      )}

      <RenameProfileDialog
        currentName={pendingRename?.name ?? ''}
        onClose={() => setPendingRename(null)}
        onRenamed={selectAndRefresh}
        open={pendingRename !== null}
      />

      <CreateProfileDialog
        onClose={() => setCreateOpen(false)}
        onCreated={selectAndRefresh}
        open={createOpen}
        profiles={profiles ?? []}
      />

      <DeleteProfileDialog
        onClose={() => setPendingDelete(null)}
        onDeleted={async () => {
          setSelectedName(null)
          await refresh()
        }}
        open={pendingDelete !== null}
        profile={pendingDelete}
      />
    </Panel>
  )
}

function ProfileRow({
  active,
  menuItems,
  onSelect,
  profile,
  avatarVersion
}: {
  active: boolean
  menuItems: PanelMenuItem[]
  onSelect: () => void
  profile: ProfileInfo
  avatarVersion: number
}) {
  const colors = useStore($profileColors)

  return (
    <PanelListRow
      active={active}
      lead={
        <ProfileAvatar
          aria-hidden="true"
          avatarVersion={avatarVersion}
          color={resolveProfileColor(profile.name, colors)}
          isDefault={profile.is_default}
          name={profile.name}
          url={profile.avatar_url}
        />
      }
      menuItems={menuItems}
      menuLabel={profile.name}
      onSelect={onSelect}
      rowKey={profile.name}
      title={profile.name}
    />
  )
}

/** A profile's visual mark: the custom avatar image when set, otherwise the
 *  colored-initial glyph. Falls back to the glyph if the image fails to load.
 *
 *  `avatarVersion` is a cache-busting token: when the backend URL is unchanged
 *  (same `/api/profiles/avatar?name=X` path) but the underlying image was
 *  replaced, appending `&v=<token>` forces the <img> to reload. */
function ProfileAvatar({
  url,
  color,
  isDefault,
  name,
  avatarVersion = 0,
  className,
  ...props
}: Omit<React.ComponentProps<'span'>, 'color'> & {
  url?: null | string
  color: null | string
  isDefault: boolean
  name: string
  avatarVersion?: number
}) {
  const [broken, setBroken] = useState(false)
  const connection = useStore($connection)
  // The backend returns a relative path (`/api/profiles/avatar?name=X`), but
  // <img src> in the Electron renderer needs a full HTTP URL — the renderer
  // origin is `file://`, so a relative path would resolve wrong. Prepend the
  // connection's baseUrl (the primary backend's origin).  Append a
  // cache-busting `&v=` token so replacing the avatar immediately refreshes
  // the <img> even though the base URL is identical.
  const fullUrl = url && !broken && connection?.baseUrl
    ? `${connection.baseUrl}${url}${url.includes('?') ? '&' : '?'}v=${avatarVersion}`
    : null

  if (fullUrl) {
    return (
      <span className={className} {...props}>
        <img
          alt={name}
          className="size-6 rounded-[5px] object-cover"
          onError={() => setBroken(true)}
          src={fullUrl}
        />
      </span>
    )
  }

  return (
    <ProfileGlyph
      className={className}
      color={color}
      isDefault={isDefault}
      name={name}
      {...props}
    />
  )
}

function ProfileDetail({
  profile,
  avatarVersion,
  onAvatarChanged
}: {
  profile: ProfileInfo
  avatarVersion: number
  onAvatarChanged: () => Promise<void>
}) {
  const { t } = useI18n()
  const p = t.profiles
  const [avatarBusy, setAvatarBusy] = useState(false)
  const [batchProgress, setBatchProgress] = useState<null | { done: number; total: number }>(null)
  const [previewOpen, setPreviewOpen] = useState(false)
  const fileInputRef = useRef<null | HTMLInputElement>(null)
  const connection = useStore($connection)
  const avatarSrc = profile.avatar_url && connection?.baseUrl
    ? `${connection.baseUrl}${profile.avatar_url}${profile.avatar_url.includes('?') ? '&' : '?'}v=${avatarVersion}`
    : null

  // Upload a single file as this profile's avatar. Returns true on success.
  async function uploadOne(file: File): Promise<boolean> {
    if (!/\.(png|jpe?g|webp|gif)$/i.test(file.name || '')) {
      notifyError(new Error(p.avatarInvalidType), p.failedLoad)
      return false
    }
    if (file.size > 5 * 1024 * 1024) {
      notifyError(new Error(p.avatarTooLarge), p.failedLoad)
      return false
    }
    const result = await uploadProfileAvatar(profile.name, file)
    if (!result.ok) {
      throw new Error(result.error || 'Avatar upload failed')
    }
    return true
  }

  // Single-file upload (the classic "Set avatar…" button).
  async function handleAvatarUpload(file: null | File) {
    if (!file) return
    setAvatarBusy(true)
    try {
      await uploadOne(file)
      notify({ kind: 'success', title: p.avatarSaved, message: profile.name })
      await onAvatarChanged()
    } catch (err) {
      notifyError(err, p.failedLoad)
    } finally {
      setAvatarBusy(false)
      if (fileInputRef.current) fileInputRef.current.value = ''
    }
  }

  // Batch upload: each selected file is uploaded to this profile in sequence
  // (last-one-wins, same as clicking "Set avatar…" N times). The progress
  // indicator shows done/total so the user can see it working.
  async function handleBatchUpload(files: FileList | null) {
    if (!files || files.length === 0) return
    if (files.length === 1) {
      await handleAvatarUpload(files[0])
      return
    }
    setAvatarBusy(true)
    setBatchProgress({ done: 0, total: files.length })
    let successCount = 0
    try {
      for (let i = 0; i < files.length; i++) {
        try {
          await uploadOne(files[i])
          successCount++
        } catch (err) {
          notifyError(err, p.failedLoad)
        }
        setBatchProgress({ done: i + 1, total: files.length })
      }
      if (successCount > 0) {
        notify({ kind: 'success', title: p.avatarSaved, message: `${profile.name} (${successCount}/${files.length})` })
        await onAvatarChanged()
      }
    } finally {
      setAvatarBusy(false)
      setBatchProgress(null)
      if (fileInputRef.current) fileInputRef.current.value = ''
    }
  }

  async function handleAvatarRemove() {
    setAvatarBusy(true)
    try {
      const result = await removeProfileAvatar(profile.name)
      if (!result.ok) {
        throw new Error(result.error || 'Avatar remove failed')
      }
      notify({ kind: 'success', title: p.avatarRemoved, message: profile.name })
      await onAvatarChanged()
    } catch (err) {
      notifyError(err, p.failedLoad)
    } finally {
      setAvatarBusy(false)
    }
  }

  const profileColors = useStore($profileColors)

  return (
    <PanelDetail>
      <header className="space-y-3">
        <div className="flex items-start gap-3">
          <div className="relative shrink-0">
            <div
              className="grid size-14 place-items-center overflow-hidden rounded-xl border border-border bg-muted"
              onClick={() => avatarSrc && setPreviewOpen(true)}
              role={avatarSrc ? 'button' : undefined}
              style={avatarSrc ? { cursor: 'pointer' } : undefined}
              title={avatarSrc ? p.avatarClickToEnlarge : undefined}
            >
              {avatarSrc ? (
                <img
                  alt={profile.name}
                  className="size-full object-cover"
                  src={avatarSrc}
                />
              ) : (
                <ProfileGlyph
                  className="size-14"
                  color={resolveProfileColor(profile.name, profileColors)}
                  isDefault={profile.is_default}
                  name={profile.name}
                />
              )}
            </div>
          </div>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="text-[0.95rem] font-semibold tracking-tight text-foreground">{profile.name}</h3>
              {profile.is_default && <PanelPill tone="good">{p.defaultBadge}</PanelPill>}
              {profile.has_env && <PanelPill tone="muted">.env</PanelPill>}
            </div>
            <p
              className="mt-1 truncate font-mono text-[0.66rem] text-muted-foreground/55"
              title={displayPath(profile.path)}
            >
              {displayPath(profile.path)}
            </p>
            <div className="mt-1.5 flex items-center gap-2">
              <Button
                disabled={avatarBusy}
                onClick={() => fileInputRef.current?.click()}
                size="sm"
                variant="secondary"
              >
                {avatarBusy
                  ? batchProgress
                    ? p.avatarUploadBatchProgress(batchProgress.done, batchProgress.total)
                    : p.avatarUploading
                  : p.setAvatar}
              </Button>
              {avatarSrc ? (
                <Button
                  disabled={avatarBusy}
                  onClick={() => void handleAvatarRemove()}
                  size="sm"
                  variant="ghost"
                >
                  {p.removeAvatar}
                </Button>
              ) : null}
              <input
                accept="image/png,image/jpeg,image/webp,image/gif"
                className="hidden"
                multiple
                onChange={event => void handleBatchUpload(event.target.files)}
                ref={fileInputRef}
                type="file"
              />
            </div>
          </div>
        </div>

        <PanelMeta
          rows={[
            {
              label: p.modelLabel,
              value: profile.model ? (
                <span className="font-mono">
                  {profile.model}
                  {profile.provider ? <span className="text-muted-foreground/55"> · {profile.provider}</span> : null}
                </span>
              ) : (
                <span className="text-muted-foreground/55">{p.notSet}</span>
              )
            },
            { label: p.skillsLabel, value: profile.skill_count }
          ]}
        />
      </header>

      <SoulEditor profileName={profile.name} />

      {previewOpen && avatarSrc && (
        <AvatarPreviewModal
          alt={profile.name}
          onClose={() => setPreviewOpen(false)}
          src={avatarSrc}
        />
      )}
    </PanelDetail>
  )
}

/** Full-screen avatar preview overlay. Click anywhere or press Escape to
 *  close. The image is centered, scaled to fit the viewport with a max of
 *  90vmin, and shown on a semi-opaque backdrop. */
function AvatarPreviewModal({
  src,
  alt,
  onClose
}: {
  src: string
  alt: string
  onClose: () => void
}) {
  const { t } = useI18n()
  const p = t.profiles

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
    >
      <img
        alt={alt}
        className="max-h-[90vh] max-w-[90vw] rounded-xl object-contain shadow-2xl"
        onClick={e => e.stopPropagation()}
        src={src}
      />
      <button
        className="absolute right-4 top-4 grid size-9 place-items-center rounded-full bg-white/10 text-white transition hover:bg-white/20"
        onClick={onClose}
        aria-label={p.avatarClosePreview}
      >
        <span className="text-lg">✕</span>
      </button>
    </div>
  )
}

function SoulEditor({ profileName }: { profileName: string }) {
  const { t } = useI18n()
  const p = t.profiles
  const [content, setContent] = useState('')
  const [original, setOriginal] = useState('')
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<null | string>(null)
  const requestRef = useRef<string>(profileName)

  // eslint-disable-next-line no-restricted-syntax -- legitimate non-atom ref write (see eslint rule comment)
  useEffect(() => {
    requestRef.current = profileName
    setLoading(true)
    setError(null)
    setContent('')
    setOriginal('')

    void (async () => {
      try {
        const soul = await getProfileSoul(profileName)

        if (requestRef.current === profileName) {
          setContent(soul.content)
          setOriginal(soul.content)
        }
      } catch (err) {
        if (requestRef.current === profileName) {
          setError(err instanceof Error ? err.message : p.failedLoadSoul)
        }
      } finally {
        if (requestRef.current === profileName) {
          setLoading(false)
        }
      }
    })()
  }, [p, profileName])

  const dirty = content !== original

  async function handleSave() {
    setSaving(true)
    setError(null)

    try {
      await updateProfileSoul(profileName, content)
      setOriginal(content)
      notify({ kind: 'success', title: p.soulSaved, message: profileName })
    } catch (err) {
      setError(err instanceof Error ? err.message : p.failedSaveSoul)
    } finally {
      setSaving(false)
    }
  }

  return (
    <section className="space-y-2">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <PanelSectionLabel className="text-[0.7rem] tracking-[0.14em]">SOUL.md</PanelSectionLabel>
          <p className="text-xs text-muted-foreground">{p.soulDesc}</p>
        </div>
        {dirty && <span className="text-[0.65rem] text-muted-foreground">{p.unsavedChanges}</span>}
      </div>

      {loading ? (
        <PageLoader className="min-h-44" label={p.loadingSoul} />
      ) : (
        <div className="min-h-48">
          <CodeEditor
            filePath="SOUL.md"
            framed
            initialValue={content}
            key={profileName}
            onChange={setContent}
            onSave={() => void handleSave()}
          />
        </div>
      )}

      {error && (
        <div className="flex items-start gap-2 rounded bg-destructive/10 px-3 py-2 text-xs text-destructive">
          <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
          <span>{error}</span>
        </div>
      )}

      <div className="flex justify-end">
        <Button disabled={!dirty || saving || loading} onClick={() => void handleSave()} size="sm">
          <Save />
          {saving ? p.saving : p.saveSoul}
        </Button>
      </div>
    </section>
  )
}
