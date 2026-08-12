/**
 * Plugin-scoped i18n for crews — bundles shipped under the plugin id via
 * ctx.i18n.register, never touching core en.ts.
 */
import { type PluginLocaleBundles, usePluginI18n } from '@hermes/plugin-sdk'
import { useMemo } from 'react'

type CrewsMessages = {
  nav: string
  open: string
  newCrewCommand: string
  title: string
  empty: string
  emptyBody: string
  newCrew: string
  back: string
  crewName: string
  goal: string
  goalPlaceholder: string
  members: (n: number) => string
  dispatch: string
  dispatchTo: string
  dispatchPlaceholder: string
  allMembers: string
  running: string
  idle: string
  done: string
  error: string
  activity: string
  noActivity: string
  workflow: string
  overview: string
  workflowEmpty: string
  workflowEmptyBody: string
  addTask: string
  connect: string
  connectHint: string
  autoLayout: string
  runWorkflow: string
  runningNow: string
  save: string
  saved: string
  delete: string
  clone: string
  deleteConfirm: string
  taskLabel: string
  taskLabelPlaceholder: string
  taskPrompt: string
  taskPromptPlaceholder: string
  assignee: string
  dependencies: string
  status: string
  newTask: string
  cancel: string
  create: string
  templates: string
  templateGoal: (name: string) => string
  persona: string
  model: string
  modelPlaceholder: string
  profile: string
  profilePlaceholder: string
  profileHint: string
  addMember: string
  membersCount: (n: number) => string
  memberPlaceholder: string
  noMembers: string
  noMembersHint: string
  dispatchFailed: string
  createFailed: string
  runFailed: string
  noCrewSelected: string
  updated: string
  notInstalled: string
  notInstalledBody: string
  workerLog: string
}

const en: CrewsMessages = {
  nav: 'Crews',
  open: 'Crews: Open crews',
  newCrewCommand: 'Crews: New crew',
  title: 'Crews',
  empty: 'No crews yet',
  emptyBody:
    'Create a crew of specialised agents — each member runs in its own Hermes profile with an isolated workspace.',
  newCrew: 'New crew',
  back: 'All crews',
  crewName: 'Name',
  goal: 'Goal',
  goalPlaceholder: 'What is this crew working toward?',
  members: n => `${n} members`,
  dispatch: 'Dispatch task',
  dispatchTo: 'Dispatch to',
  dispatchPlaceholder: 'Describe the task for the crew…',
  allMembers: 'All members',
  running: 'running',
  idle: 'idle',
  done: 'done',
  error: 'error',
  activity: 'Activity',
  noActivity: 'No activity yet — dispatch a task to see the feed.',
  workflow: 'Workflow',
  overview: 'Overview',
  workflowEmpty: 'No workflow yet',
  workflowEmptyBody: 'Build a DAG of tasks — each task runs as a one-shot agent prompt in topological order.',
  addTask: 'Add task',
  connect: 'Connect',
  connectHint: 'Click a task, then click another to add a dependency',
  autoLayout: 'Auto layout',
  runWorkflow: 'Run workflow',
  runningNow: 'Running…',
  save: 'Save',
  saved: 'Saved',
  delete: 'Delete',
  clone: 'Clone',
  deleteConfirm: 'Delete this crew?',
  taskLabel: 'Label',
  taskLabelPlaceholder: 'e.g. Research API surface',
  taskPrompt: 'Prompt',
  taskPromptPlaceholder: 'Full prompt sent to the assignee agent…',
  assignee: 'Assignee',
  dependencies: 'Dependencies',
  status: 'Status',
  newTask: 'New task',
  cancel: 'Cancel',
  create: 'Create',
  templates: 'Templates',
  templateGoal: name => `Pre-fill with ${name}`,
  persona: 'Persona',
  model: 'Model',
  modelPlaceholder: 'e.g. auto (leave empty for profile default)',
  profile: 'Profile',
  profilePlaceholder: 'hermes profile name (defaults to the persona)',
  profileHint: 'Each member runs in its own profile with an isolated workspace.',
  addMember: 'Add member',
  membersCount: n => `${n} of 8 members`,
  memberPlaceholder: 'Pick a persona',
  noMembers: 'No members yet',
  noMembersHint: 'Add at least one persona to dispatch work.',
  dispatchFailed: 'Dispatch failed',
  createFailed: 'Could not create crew',
  runFailed: 'Could not start workflow run',
  noCrewSelected: 'Select a crew',
  updated: 'updated',
  notInstalled: 'Crews backend is not mounted',
  notInstalledBody:
    'Enable the crews plugin (Settings → Plugins) and restart the gateway, or run hermes with the dashboard server.',
  workerLog: 'Worker log'
}

const ja: CrewsMessages = {
  nav: 'クルー',
  open: 'クルー: クルーを開く',
  newCrewCommand: 'クルー: 新しいクルー',
  title: 'クルー',
  empty: 'クルーがまだありません',
  emptyBody:
    '専門エージェントのチームを作成 — 各メンバーは独自の Hermes プロファイルで実行され、作業領域が分離されます。',
  newCrew: '新しいクルー',
  back: 'すべてのクルー',
  crewName: '名前',
  goal: '目標',
  goalPlaceholder: 'このクルーが目指すものは？',
  members: n => `${n} 人のメンバー`,
  dispatch: 'タスクを配信',
  dispatchTo: '配信先',
  dispatchPlaceholder: 'クルーへのタスクを説明…',
  allMembers: '全メンバー',
  running: '実行中',
  idle: '待機中',
  done: '完了',
  error: 'エラー',
  activity: 'アクティビティ',
  noActivity: 'まだアクティビティがありません — タスクを配信するとフィードが表示されます。',
  workflow: 'ワークフロー',
  overview: '概要',
  workflowEmpty: 'ワークフローがまだありません',
  workflowEmptyBody:
    'タスクのDAGを構築 — 各タスクはトポロジカル順にワンショットのエージェントプロンプトとして実行されます。',
  addTask: 'タスクを追加',
  connect: '接続',
  connectHint: 'タスクをクリックし、次に別のタスクをクリックして依存関係を追加',
  autoLayout: '自動レイアウト',
  runWorkflow: 'ワークフローを実行',
  runningNow: '実行中…',
  save: '保存',
  saved: '保存済み',
  delete: '削除',
  clone: '複製',
  deleteConfirm: 'このクルーを削除しますか？',
  taskLabel: 'ラベル',
  taskLabelPlaceholder: '例: API サーフェスを調査',
  taskPrompt: 'プロンプト',
  taskPromptPlaceholder: '担当エージェントに送る完全なプロンプト…',
  assignee: '担当',
  dependencies: '依存関係',
  status: 'ステータス',
  newTask: '新しいタスク',
  cancel: 'キャンセル',
  create: '作成',
  templates: 'テンプレート',
  templateGoal: name => `${name} でプリフィル`,
  persona: 'ペルソナ',
  model: 'モデル',
  modelPlaceholder: '例: auto（空欄でプロファイル既定）',
  profile: 'プロファイル',
  profilePlaceholder: 'hermes プロファイル名（既定はペルソナ）',
  profileHint: '各メンバーは独自のプロファイルで実行され、作業領域が分離されます。',
  addMember: 'メンバーを追加',
  membersCount: n => `${n} / 8 メンバー`,
  memberPlaceholder: 'ペルソナを選択',
  noMembers: 'メンバーがいません',
  noMembersHint: 'タスクを配信するには少なくとも1人のペルソナを追加してください。',
  dispatchFailed: '配信に失敗しました',
  createFailed: 'クルーを作成できませんでした',
  runFailed: 'ワークフローの実行を開始できませんでした',
  noCrewSelected: 'クルーを選択',
  updated: '更新',
  notInstalled: 'Crews バックエンドがマウントされていません',
  notInstalledBody:
    'クループラグインを有効化（設定 → プラグイン）してゲートウェイを再起動するか、ダッシュボードサーバー付きで hermes を実行してください。',
  workerLog: 'ワーカーログ'
}

export const CREWS_LOCALES: PluginLocaleBundles = { en, ja }

export function useCrewsI18n(): CrewsMessages {
  const t = usePluginI18n('crews')

  return useMemo(() => t as unknown as CrewsMessages, [t])
}
