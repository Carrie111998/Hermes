// app/build.gradle.kts
android {
    // ... altre configurazioni ...

    defaultConfig {
        // ...
        // Recupera la chiave dalle variabili d'ambiente (ottimo per la CI/CD)
        val geminiApiKey = System.getenv("GEMINI_API_KEY") ?: ""
        buildConfigField("String", "GEMINI_API_KEY", "\"$geminiApiKey\"")
    }

    buildFeatures {
        // CORREZIONE: Abilita la generazione della classe BuildConfig
        buildConfig = true
    }
}