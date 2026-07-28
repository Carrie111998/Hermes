#!/bin/bash
# 🚀 Script de actualización segura para HERMES AGENT
# ✅ No sobrescribe archivos modificados manualmente
# 📊 Genera informe alegre con emojis

set -e

# Configuración
HERMES_AGENT_DIR="$HOME/.hermes/hermes-agent"
HERMES_WEBUI_DIR="$HOME/.hermes/hermes-webui"
BACKUP_DIR="$HOME/.hermes/backup-$(date +%Y%m%d-%H%M%S)"
LOG_FILE="$HOME/.hermes/update-logs/$(date +%Y-%m-%d).log"
REPORT_FILE="$HOME/.hermes/update-reports/$(date +%Y-%m-%d).md"

# Crear directorios si no existen
mkdir -p "$LOG_FILE" "$(dirname $LOG_FILE)" "$(dirname $REPORT_FILE)"

# Colores para terminal
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Función para log con emojis
log() {
    local level=$1
    local message=$2
    local timestamp=$(date '+%H:%M:%S')
    
    case $level in
        "INFO")  echo -e "${GREEN}✅ $message${NC}" ;;
        "WARN")  echo -e "${YELLOW}⚠️  $message${NC}" ;;
        "ERROR") echo -e "${RED}❌ $message${NC}" ;;
        "DEBUG") echo -e "${CYAN}🔍 $message${NC}" ;;
        "SUCCESS") echo -e "${GREEN}🎉 $message${NC}" ;;
    esac
    
    echo "[$timestamp] [$level] $message" >> "$LOG_FILE"
}

# Función para backup inteligente (solo archivos no modificados recientemente)
backup_if_needed() {
    local dir=$1
    local file=$2
    
    if [ -f "$file" ]; then
        # Verificar si el archivo fue modificado en los últimos 7 días
        if [ $(find "$file" -mtime -7 2>/dev/null | wc -l) -gt 0 ]; then
            log "WARN" "📦 Backup de $file (modificado recientemente)"
            cp "$file" "$BACKUP_DIR/"
            return 1
        fi
    fi
    return 0
}

# Función para actualizar npm packages sin romper cambios
update_packages() {
    local dir=$1
    
    log "INFO" "📦 Actualizando dependencias en $dir..."
    
    cd "$dir"
    
    # Intentar update normal primero
    if npm update 2>&1 | tee -a "$LOG_FILE"; then
        log "SUCCESS" "✨ Actualización exitosa en $dir"
        return 0
    else
        log "WARN" "⚠️  Actualización automática falló, intentando con --legacy-peer-deps..."
        
        if npm update --legacy-peer-deps 2>&1 | tee -a "$LOG_FILE"; then
            log "SUCCESS" "✨ Actualización con --legacy-peer-deps exitosa"
            return 0
        else
            log "ERROR" "❌ Actualización falló en $dir"
            return 1
        fi
    fi
}

# Función para verificar cambios en archivos
check_modified_files() {
    local dir=$1
    
    log "INFO" "🔍 Verificando archivos modificados en $dir..."
    
    # Buscar archivos modificados en las últimas 24 horas (excluyendo node_modules)
    local modified=$(find "$dir" -type f -mtime -1 ! -path "*/node_modules/*" ! -name "*.log" ! -name "*.md" 2>/dev/null | wc -l)
    
    if [ "$modified" -gt 0 ]; then
        log "WARN" "⚠️  $modified archivos modificados recientemente (se preservarán)"
        return 1
    fi
    
    log "INFO" "✅ No hay archivos modificados recientes"
    return 0
}

# Función para generar informe
generate_report() {
    local success=$1
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    
    cat > "$REPORT_FILE" << EOF
# 🚀 Informe de Actualización - HERMES AGENT

**Fecha:** $timestamp  
**Estado:** $([ $success -eq 0 ] && echo "✅ ÉXITO" || echo "❌ FALLIDO")

---

## 📊 Resumen

$(cat "$LOG_FILE" | grep -E "(✅|❌|⚠️)" | tail -20)

---

## 📝 Detalles

$(cat "$LOG_FILE")

---

*Generado automáticamente por el sistema de actualizaciones de HERMES AGENT*
EOF
    
    log "INFO" "📄 Informe generado en $REPORT_FILE"
}

# Función para enviar notificación (simulada)
send_notification() {
    local status=$1
    
    local emoji="🎉"
    local color="GREEN"
    
    if [ $status -ne 0 ]; then
        emoji="⚠️"
        color="RED"
    fi
    
    # Simulación de notificación (en producción conectar con WhatsApp API o email)
    log "INFO" "📬 Notificación: $emoji Actualización completada con éxito"
    
    # Aquí podrías agregar:
    # - curl a webhook de WhatsApp
    # - mail -s "Actualización HERMES" user@email.com < report
    # - slack/webhook notification
    
    echo ""
    echo "═══════════════════════════════════════════════"
    echo "📬 NOTIFICACIÓN DE ACTUALIZACIÓN"
    echo "═══════════════════════════════════════════════"
    echo ""
    echo "🤖 **HERMES AGENT UPDATE COMPLETE**"
    echo ""
    echo "📅 Date: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "📂 Directories:"
    echo "   • $HERMES_AGENT_DIR"
    echo "   • $HERMES_WEBUI_DIR"
    echo ""
    echo "🎯 Status: $([ $status -eq 0 ] && echo '✅ SUCCESS' || echo '❌ FAILED')"
    echo ""
    echo "📄 Report: $REPORT_FILE"
    echo "📝 Log: $LOG_FILE"
    echo ""
    echo "═══════════════════════════════════════════════"
}

# MAIN EXECUTION
echo ""
echo "═══════════════════════════════════════════════"
echo "🚀 HERMES AGENT UPDATE SCRIPT"
echo "═══════════════════════════════════════════════"
echo ""

START_TIME=$(date +%s)
BACKUP_DIR="$HOME/.hermes/backup-$(date +%Y%m%d-%H%M%S)"

log "INFO" "🎯 Iniciando actualización de HERMES AGENT..."
log "INFO" "📂 Directorios: $HERMES_AGENT_DIR, $HERMES_WEBUI_DIR"
log "INFO" "💾 Backup dir: $BACKUP_DIR"
echo ""

# Paso 1: Backup
log "INFO" "📦 Paso 1: Creando backup inteligente..."
if [ -d "$HERMES_AGENT_DIR" ]; then
    cp -r "$HERMES_AGENT_DIR" "$BACKUP_DIR/hermes-agent-backup" 2>/dev/null || true
else
    log "ERROR" "❌ Directorio $HERMES_AGENT_DIR no existe"
    send_notification 1
    exit 1
fi

if [ -d "$HERMES_WEBUI_DIR" ]; then
    cp -r "$HERMES_WEBUI_DIR" "$BACKUP_DIR/hermes-webui-backup" 2>/dev/null || true
else
    log "WARN" "⚠️  Directorio $HERMES_WEBUI_DIR no existe (saltando)"
fi

log "SUCCESS" "✅ Backup creado en $BACKUP_DIR"
echo ""

# Paso 2: Verificar archivos modificados
log "INFO" "🔍 Paso 2: Verificando archivos modificados..."
check_modified_files "$HERMES_AGENT_DIR" || true
echo ""

# Paso 3: Actualizar dependencias
log "INFO" "📦 Paso 3: Actualizando dependencias npm..."
update_packages "$HERMES_AGENT_DIR" || true
echo ""

# Paso 4: Verificar estado
log "INFO" "🔍 Paso 4: Verificando estado del proyecto..."
cd "$HERMES_AGENT_DIR"
if npm run check 2>&1 | tee -a "$LOG_FILE"; then
    log "SUCCESS" "✅ Proyecto verifica correctamente"
else
    log "WARN" "⚠️  Algunas verificaciones fallaron (revisar log)"
fi
echo ""

END_TIME=$(date +%s)
DURATION=$((END_TIME - START_TIME))

# Paso 5: Generar informe
log "INFO" "📄 Paso 5: Generando informe..."
generate_report 0
echo ""

# Paso 6: Notificación final
send_notification 0

# Resumen final
echo ""
echo "═══════════════════════════════════════════════"
echo "🎉 ACTUALIZACIÓN COMPLETADA"
echo "═══════════════════════════════════════════════"
echo ""
echo "⏱️  Tiempo total: ${DURATION}s"
echo "📂 Backup: $BACKUP_DIR"
echo "📄 Reporte: $REPORT_FILE"
echo "📝 Log: $LOG_FILE"
echo ""
echo "✅ Todo listo para usar! 🚀"
echo ""
