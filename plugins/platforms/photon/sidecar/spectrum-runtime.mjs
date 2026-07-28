const defaultImporter = (specifier) => import(specifier);

export async function createSpectrumRuntime({
  localMode,
  projectId,
  projectSecret,
  telemetry,
  importer = defaultImporter,
}) {
  const core = await importer("@spectrum-ts/core");
  const providerModule = await importer(
    localMode ? "@spectrum-ts/imessage-local" : "spectrum-ts/providers/imessage"
  );
  const imessage = localMode
    ? providerModule.localIMessage
    : providerModule.imessage;

  if (typeof core.Spectrum !== "function" || typeof imessage?.config !== "function") {
    throw new TypeError("The installed Spectrum packages do not expose the expected API.");
  }

  const config = {
    providers: [imessage.config()],
    options: { flattenGroups: true },
    telemetry,
  };
  if (!localMode) {
    config.projectId = projectId;
    config.projectSecret = projectSecret;
  }

  return {
    app: await core.Spectrum(config),
    attachment: core.attachment,
    voice: core.voice,
    spectrumText: core.text,
    spectrumMarkdown: core.markdown,
    spectrumTyping: core.typing,
  };
}
