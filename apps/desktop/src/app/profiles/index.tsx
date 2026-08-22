import { useStore } from '@nanostores/react'
import type * as React from 'react'
import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { CodeEditor } from '@/components/chat/code-editor'
import { PageLoader } from '@/components/page-loader'
import { Button } from '@/components/ui/button'
import { ProfileGlyph } from '@/components/ui/profile-glyph'
import { getProfiles, getProfileSoul, type ProfileInfo, updateProfileSoul } from '@/hermes'
import { useI18n } from '@/i18n'
import { displayPath } from '@/lib/display-path'
import { AlertTriangle, Save } from '@/lib/icons'
import { resolveProfileColor } from '@/lib/profile-color'
import { normalize } from '@/lib/text'
import {
  $connectionLabels,
  $multiGateway,
  $primaryConnectionId,
  agentKey,
  LOCAL_CONNECTION_ID
} from '@/store/gateway-separation'
import { notify, notifyError } from '@/store/notifications'
import { $profileColors, profileLabel, refreshProfiles } from '@/store/profile'

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

/** One row's full identity: a profile plus
 *  the machine that owns it. Two registered gateways both serve a `default`, so
 *  a name on its own can neither identify a row nor address an action. */
interface ProfileEntry {
  /** '' for the primary / local pool — the byte-identical single-gateway case. */
  connectionId: string
  connectionLabel: string
  profile: ProfileInfo
}

const entryKey = (entry: ProfileEntry) => agentKey(entry.connectionId, entry.profile.name)

export function ProfilesView({ onClose }: ProfilesViewProps) {
  const { t } = useI18n()
  const p = t.profiles
  const [entries, setEntries] = useState<null | ProfileEntry[]>(null)
  const [selectedKey, setSelectedKey] = useState<null | string>(null)
  const [query, setQuery] = useState('')
  const [createOn, setCreateOn] = useState<null | string>(null)
  const [pendingRename, setPendingRename] = useState<null | ProfileEntry>(null)
  const [pendingDelete, setPendingDelete] = useState<null | ProfileEntry>(null)

  // Every registered gateway, primary first. Manage Profiles used
  // to call `refreshProfiles()` alone, which is hard-wired to the primary, so a
  // second machine's agents were simply absent from the one screen that exists
  // to manage them.
  const multiGateway = useStore($multiGateway)
  const connectionLabels = useStore($connectionLabels)
  const primaryConnectionId = useStore($primaryConnectionId)

  // The primary carries its REAL registry id, never ''.
  //
  // An omitted connection means "whichever gateway is ambient", NOT "the
  // registry primary" — `hermesApi` spreads `connectionScoped()`. Encoding the
  // primary as '' therefore made the section LABELLED with the primary's name
  // read, and write to, whatever machine happened to be attached: open Manage
  // Profiles from a secondary and the primary's section listed the secondary's
  // profiles, while create/rename/delete under that heading hit the secondary
  // too. Addressing every source explicitly is what keeps a row's machine and
  // its label the same machine.
  const primaryId = (primaryConnectionId || LOCAL_CONNECTION_ID).trim()

  const sources = useMemo(() => {
    const primaryLabel = connectionLabels[primaryId] || ''
    const rest = Object.keys(connectionLabels).filter(id => id !== primaryId && id !== LOCAL_CONNECTION_ID)

    return [
      { connectionId: primaryId, label: primaryLabel },
      ...rest.map(id => ({ connectionId: id, label: connectionLabels[id] || id }))
    ]
  }, [connectionLabels, primaryId])

  const refresh = useCallback(async () => {
    // Best-effort per source: an unreachable machine contributes nothing rather
    // than emptying the whole panel, matching how the roster degrades.
    const perSource = await Promise.all(
      sources.map(async source => {
        try {
          // Explicit id for every source once a second gateway exists. A
          // single-gateway install keeps upstream's exact ambient call, so
          // nothing changes for it.
          const list = multiGateway ? (await getProfiles(source.connectionId)).profiles : await refreshProfiles()

          return list.map(profile => ({
            connectionId: source.connectionId,
            connectionLabel: source.label,
            profile
          }))
        } catch (err) {
          // Only the primary failing is worth interrupting the user for; a
          // secondary gateway being down is expected and already visible as an
          // absent section.
          if (source.connectionId === primaryId) {
            notifyError(err, p.failedLoad)
          }

          return [] as ProfileEntry[]
        }
      })
    )

    const flat = perSource.flat()
    setEntries(flat)
    setSelectedKey(current => {
      if (current && flat.some(entry => entryKey(entry) === current)) {
        return current
      }

      const fallback = flat.find(entry => entry.connectionId === primaryId && entry.profile.is_default) ?? flat[0]

      return fallback ? entryKey(fallback) : null
    })

    // The rail's own cache is ambient by design (it describes the attached
    // machine), so keep it current alongside the explicit per-source reads.
    if (multiGateway) {
      void refreshProfiles().catch(() => undefined)
    }
  }, [multiGateway, p, primaryId, sources])

  useRefreshHotkey(refresh)

  useEffect(() => {
    void refresh()
  }, [refresh])

  const selected = useMemo(() => {
    if (!entries) {
      return null
    }

    return entries.find(entry => entryKey(entry) === selectedKey) ?? entries[0] ?? null
  }, [entries, selectedKey])

  const visibleEntries = useMemo(() => {
    const q = normalize(query)

    if (!entries || !q) {
      return entries ?? []
    }

    return entries.filter(
      entry =>
        entry.profile.name.toLowerCase().includes(q) ||
        (entry.profile.model ?? '').toLowerCase().includes(q) ||
        entry.connectionLabel.toLowerCase().includes(q)
    )
  }, [entries, query])

  // One section per machine while more than one gateway is registered; a single
  // unlabelled run of rows otherwise, exactly as upstream renders it.
  const sections = useMemo(
    () =>
      sources
        .map(source => ({
          ...source,
          rows: visibleEntries.filter(entry => entry.connectionId === source.connectionId)
        }))
        .filter(section => section.rows.length > 0 || section.connectionId === primaryId),
    [primaryId, sources, visibleEntries]
  )

  // Internally every source carries its real id, so a row's machine and its
  // label can never drift. On the WIRE, a single-gateway install must still
  // issue upstream's byte-identical ambient call — '' means "no override".
  const scopeFor = (id?: null | string): string => (multiGateway ? ((id ?? '').trim() || primaryId) : '')

  // The shared Create/Rename dialogs own the createProfile / renameProfile /
  // updateProfileSoul calls; the panel just selects the resulting profile and
  // re-pulls the list.
  const selectAndRefresh = useCallback(
    async (connectionId: string, name: string) => {
      setSelectedKey(agentKey(connectionId, name))
      await refresh()
    },
    [refresh]
  )

  const menuFor = (entry: ProfileEntry): PanelMenuItem[] =>
    entry.profile.is_default
      ? // Renaming the default profile sets a presentation-only display name
        // (the canonical id stays "default").
        [{ icon: 'edit', label: p.renameMenu, onSelect: () => setPendingRename(entry) }]
      : [
          { icon: 'edit', label: p.renameMenu, onSelect: () => setPendingRename(entry) },
          {
            icon: 'trash',
            label: t.common.delete,
            onSelect: () => setPendingDelete(entry),
            tone: 'danger'
          }
        ]

  return (
    <Panel closeLabel={p.close} onClose={onClose}>
      {!entries ? (
        <PageLoader label={p.loading} />
      ) : entries.length === 0 ? (
        <PanelEmpty
          action={
            <Button onClick={() => setCreateOn('')} size="sm">
              {p.newProfile}
            </Button>
          }
          description={p.createDesc}
          icon="organization"
          title={p.noProfiles}
        />
      ) : (
        <>
          <PanelHeader subtitle={p.count(entries.length)} title={p.title} />
          <PanelBody>
            <PanelList
              onSearchChange={setQuery}
              searchLabel={p.search}
              searchPlaceholder={p.search}
              searchValue={query}
            >
              {sections.map(section => (
                <Fragment key={section.connectionId}>
                  {multiGateway && section.label ? <PanelSectionLabel>{section.label}</PanelSectionLabel> : null}
                  {section.rows.map(entry => (
                    <ProfileRow
                      active={selected ? entryKey(selected) === entryKey(entry) : false}
                      key={entryKey(entry)}
                      menuItems={menuFor(entry)}
                      onSelect={() => setSelectedKey(entryKey(entry))}
                      profile={entry.profile}
                    />
                  ))}
                  {/* Create lands on the machine whose section it sits in. */}
                  <PanelAddButton label={p.newProfile} onClick={() => setCreateOn(section.connectionId)} />
                </Fragment>
              ))}
            </PanelList>

            {selected ? (
              <ProfileDetail
                connectionId={scopeFor(selected.connectionId)}
                connectionLabel={multiGateway ? selected.connectionLabel : ''}
                key={entryKey(selected)}
                profile={selected.profile}
              />
            ) : (
              <PanelEmpty description={p.selectPrompt} icon="account" />
            )}
          </PanelBody>
        </>
      )}

      <RenameProfileDialog
        connectionId={scopeFor(pendingRename?.connectionId)}
        currentName={pendingRename?.profile.name ?? ''}
        isDefault={pendingRename?.profile.is_default ?? false}
        onClose={() => setPendingRename(null)}
        onRenamed={name => selectAndRefresh(pendingRename?.connectionId ?? primaryId, name)}
        open={pendingRename !== null}
      />

      <CreateProfileDialog
        connectionId={scopeFor(createOn)}
        onClose={() => setCreateOn(null)}
        onCreated={name => selectAndRefresh(createOn ?? primaryId, name)}
        open={createOn !== null}
        profiles={(entries ?? []).filter(entry => entry.connectionId === (createOn ?? primaryId)).map(entry => entry.profile)}
      />

      <DeleteProfileDialog
        connectionId={scopeFor(pendingDelete?.connectionId)}
        onClose={() => setPendingDelete(null)}
        onDeleted={async () => {
          setSelectedKey(null)
          await refresh()
        }}
        open={pendingDelete !== null}
        profile={pendingDelete?.profile ?? null}
      />
    </Panel>
  )
}

function ProfileRow({
  active,
  menuItems,
  onSelect,
  profile
}: {
  active: boolean
  menuItems: PanelMenuItem[]
  onSelect: () => void
  profile: ProfileInfo
}) {
  const colors = useStore($profileColors)

  return (
    <PanelListRow
      active={active}
      lead={
        <ProfileGlyph
          aria-hidden="true"
          color={resolveProfileColor(profile.name, colors)}
          isDefault={profile.is_default}
          name={profile.name}
        />
      }
      menuItems={menuItems}
      menuLabel={profileLabel(profile)}
      onSelect={onSelect}
      rowKey={profile.name}
      title={profileLabel(profile)}
    />
  )
}

function ProfileDetail({
  connectionId,
  connectionLabel,
  profile
}: {
  connectionId: string
  /** '' on a single-gateway install, which renders exactly as upstream. */
  connectionLabel: string
  profile: ProfileInfo
}) {
  const { t } = useI18n()
  const p = t.profiles

  return (
    <PanelDetail>
      <header className="space-y-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-[0.95rem] font-semibold tracking-tight text-foreground">{profileLabel(profile)}</h3>
            {profile.is_default && <PanelPill tone="good">{p.defaultBadge}</PanelPill>}
            {profile.has_env && <PanelPill tone="muted">.env</PanelPill>}
            {/* Which machine this profile lives on — the path below is a path
                on THAT box, and two gateways' `default` are otherwise
                indistinguishable here. */}
            {connectionLabel ? <PanelPill tone="muted">{connectionLabel}</PanelPill> : null}
          </div>
          <p
            className="mt-1 truncate font-mono text-[0.66rem] text-muted-foreground/55"
            title={displayPath(profile.path)}
          >
            {displayPath(profile.path)}
          </p>
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

      <SoulEditor connectionId={connectionId} profileName={profile.name} />
    </PanelDetail>
  )
}

function SoulEditor({ connectionId, profileName }: { connectionId: string; profileName: string }) {
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
        const soul = await getProfileSoul(profileName, connectionId)

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
  }, [connectionId, p, profileName])

  const dirty = content !== original

  async function handleSave() {
    setSaving(true)
    setError(null)

    try {
      await updateProfileSoul(profileName, content, connectionId)
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
