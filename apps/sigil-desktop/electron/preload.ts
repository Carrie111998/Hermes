import { contextBridge, ipcRenderer } from 'electron'

const SIGIL_BACKEND_STATUS_CHANNEL = 'sigil:get-backend-status'
const SIGIL_EXPLAIN_PROPOSAL_CHANNEL = 'sigil:explain-proposal'

contextBridge.exposeInMainWorld('sigilDesktop', {
  productName: 'Sigil',
  persistenceNamespace: 'com.firecattechnology.sigil',
  brokerSubmissionAvailable: false,
  getBackendStatus: () => ipcRenderer.invoke(SIGIL_BACKEND_STATUS_CHANNEL),
  explainProposal: (payload: Readonly<Record<string, unknown>>) =>
    ipcRenderer.invoke(SIGIL_EXPLAIN_PROPOSAL_CHANNEL, payload)
})
