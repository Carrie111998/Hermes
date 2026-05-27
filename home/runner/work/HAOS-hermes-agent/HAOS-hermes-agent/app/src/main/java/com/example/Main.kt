// app/build.gradle.kts
android {
    // ... altre configurazioni ...

    buildFeatures {
        // Abilita la generazione della classe BuildConfig
        buildConfig = true
    }

    defaultConfig {
        // ... altri parametri ...
        
        // Recupera la chiave dalle variabili di sistema (CI) o local.properties
        val geminiKey = System.getenv("GEMINI_API_KEY") ?: ""
        buildConfigField("String", "GEMINI_API_KEY", "\"$geminiKey\"")
    }
}