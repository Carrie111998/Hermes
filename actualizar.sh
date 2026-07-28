#!/bin/bash
echo "📦 1/3 Guardando tu parche híbrido en el escondite (stash)..."
git stash

echo "🔄 2/3 Descargando la última actualización oficial de Hermes..."
git pull

echo "💉 3/3 Reinyectando el parche de enrutamiento local..."
git stash pop

echo "✅ ¡Actualización completada! Tu parche sigue vivo."
