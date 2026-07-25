import { contextBridge } from 'electron'

contextBridge.exposeInMainWorld('sigilDesktop', {
  productName: 'Sigil',
  persistenceNamespace: 'com.firecattechnology.sigil',
  brokerSubmissionAvailable: false
})
