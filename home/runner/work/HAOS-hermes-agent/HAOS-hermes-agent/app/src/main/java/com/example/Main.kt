// In app/build.gradle.kts
android {
    compileSdk = 34 // o la tua versione attuale

    defaultConfig {
        // ... altre configurazioni ...
        
        // Esempio di come definire la chiave (può leggere da variabili d'ambiente)
        val geminiApiKey = System.getenv("GEMINI_API_KEY") ?: "\"DEFAULT_KEY\""
        buildConfigField("String", "GEMINI_API_KEY", geminiApiKey)
    }

    buildFeatures {
        // CORREZIONE: Abilita esplicitamente la generazione della classe BuildConfig
        buildConfig = true
    }
}