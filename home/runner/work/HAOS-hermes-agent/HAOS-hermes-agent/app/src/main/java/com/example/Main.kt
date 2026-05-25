// app/build.gradle.kts
android {
    // ... altre configurazioni

    defaultConfig {
        // ...
        // Recupera la chiave da ambiente o usa un placeholder
        val geminiKey = System.getenv("GEMINI_API_KEY") ?: "\"YOUR_DEFAULT_API_KEY\""
        buildConfigField("String", "GEMINI_API_KEY", geminiKey)
    }

    buildFeatures {
        // ATTENZIONE: Impostare a true per abilitare la classe BuildConfig
        buildConfig = true
    }
}