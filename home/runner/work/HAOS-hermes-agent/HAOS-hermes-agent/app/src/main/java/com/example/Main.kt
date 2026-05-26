android {
    // ... altre configurazioni ...

    defaultConfig {
        // Recupera la chiave dalle variabili d'ambiente (es. in CI) o usa una stringa vuota di fallback
        val geminiApiKey = System.getenv("GEMINI_API_KEY") ?: ""
        buildConfigField("String", "GEMINI_API_KEY", "\"$geminiApiKey\"")
    }

    buildFeatures {
        // Abilita la generazione della classe BuildConfig
        buildConfig = true
    }
}