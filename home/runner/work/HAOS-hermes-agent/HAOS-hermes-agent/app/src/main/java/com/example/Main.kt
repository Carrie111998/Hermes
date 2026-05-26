// app/build.gradle.kts
android {
    compileSdk = 34 // o la tua versione corrente

    defaultConfig {
        // ... altre configurazioni ...

        // Recupera la chiave dalle variabili d'ambiente (sicuro per la CI) o local.properties
        val geminiApiKey: String = System.getenv("GEMINI_API_KEY") 
            ?: project.findProperty("GEMINI_API_KEY")?.toString() 
            ?: ""
        
        buildConfigField("String", "GEMINI_API_KEY", "\"$geminiApiKey\"")
    }

    buildFeatures {
        // Abilita la generazione di BuildConfig
        buildConfig = true
    }
}