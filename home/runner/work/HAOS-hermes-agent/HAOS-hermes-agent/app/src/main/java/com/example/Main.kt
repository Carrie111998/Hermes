// app/build.gradle.kts
android {
    // ... altre configurazioni ...

    defaultConfig {
        // ... altre configurazioni ...
        
        // Definisce la costante leggendola dalle variabili d'ambiente o local.properties
        val geminiApiKey = System.getenv("GEMINI_API_KEY") ?: ""
        buildConfigField("String", "GEMINI_API_KEY", "\"$geminiApiKey\"")
    }

    buildFeatures {
        // CORRETTO: Abilita la generazione della classe BuildConfig
        buildConfig = true
    }
}