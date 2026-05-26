android {
    // ... altre configurazioni esistenti ...

    buildFeatures {
        // Abilita correttamente la generazione della classe BuildConfig
        buildConfig = true
    }

    defaultConfig {
        // ... altre configurazioni ...
        
        // Definisce la costante leggendola in sicurezza dalle proprietà di progetto
        val geminiApiKey = project.findProperty("GEMINI_API_KEY") ?: "CHIAVE_DI_DEFAULT"
        buildConfigField("String", "GEMINI_API_KEY", "\"$geminiApiKey\"")
    }
}