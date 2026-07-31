# CORE_PATCHES.md — Registro de toques al núcleo de Hermes

Cada vez que un cambio de Douglas Agent toca un archivo fuera de `douglas/`
(fuera de la capa de producto), se anota aquí. El objetivo es poder revisar
de un vistazo toda la superficie de fricción con `upstream/main` antes de
cada intento de `git merge upstream/main`.

Formato por entrada:

```
## <ruta del archivo>
- **Motivo**: por qué fue necesario tocar el núcleo en vez de extenderlo
  desde douglas/.
- **Alternativa descartada**: qué otra forma se consideró (plugin, hook,
  wrapper) y por qué no alcanzaba.
- **Commit**: hash del commit que lo introdujo.
```

Vacío por ahora — ningún archivo del núcleo ha sido tocado todavía.
