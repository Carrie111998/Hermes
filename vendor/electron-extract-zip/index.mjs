import extractZip from 'extract-zip'

export function extract(zipPath, options) {
  return extractZip(zipPath, options)
}
