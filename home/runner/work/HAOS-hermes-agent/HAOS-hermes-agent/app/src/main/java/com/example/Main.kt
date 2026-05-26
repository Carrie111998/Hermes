// app/build.gradle.kts
android {
    compileSdk = 34 // o la tua versione

    defaultConfig {
        applicationId = "com.example"
        // ... altre configurazioni ...

        // Definisci la costante leggendo da local.properties o ambiente CI
        val geminiApiKey = project.findProperty("GEMINI_API_KEY") ?: System.getenv("GEMINI_API_KEY") ?: ""
        buildConfigField("String", "GEMINI_API_KEY", "\"$geminiApiKey\"")
    }

    buildFeatures {
        // Abilita correttamente la generazione della classe BuildConfig
        buildConfig = true 
    }
}