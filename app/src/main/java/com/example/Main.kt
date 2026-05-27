android {
    // ... all'interno di android { ... }

    defaultConfig {
        // ... altre configurazioni
        
        // Recupera la chiave dalle variabili d'ambiente di sistema (es. GitHub Actions)
        val geminiApiKey = System.getenv("GEMINI_API_KEY") ?: ""
        buildConfigField("String", "GEMINI_API_KEY", "\"$geminiApiKey\"")
    }

    buildFeatures {
        // Corretto: abilita la generazione della classe BuildConfig
        buildConfig = true 
    }
}