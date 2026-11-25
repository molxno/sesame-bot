# Cómo trabajar en Sesame (Git Flow)

| Rama | Para qué | Sale de | Entra a |
|---|---|---|---|
| `main` | Producción. Cada merge se despliega solo en Azure. | — | — |
| `develop` | Integración de lo próximo. | `main` | — |
| `feature/<nombre>` | Funciones nuevas | `develop` | `develop` |
| `bugfix/<nombre>` | Arreglos no urgentes | `develop` | `develop` |
| `release/<versión>` | Preparar una salida a producción | `develop` | `main` (y luego `develop`) |
| `hotfix/<nombre>` | Arreglo urgente en producción | `main` | `main` y `develop` |

Reglas (las aplica GitHub, nadie puede saltárselas):
- `main` y `develop` no aceptan push directo, force push ni borrado: todo entra por Pull Request.
- Un PR solo se puede fusionar si pasan **gitflow** (nombre de rama correcto) y **test**.
- La conversación del PR debe estar resuelta.

Flujo típico:

```bash
git switch develop && git pull
git switch -c feature/mi-cambio
# ...cambios + tests: python -m pytest -q
git push -u origin feature/mi-cambio      # abrir PR -> develop
# para publicar: PR de develop vía release/AAAA.MM.DD -> main
```
