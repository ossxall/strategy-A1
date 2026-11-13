#!/usr/bin/env python3
"""Ejecuta el backtest con la configuración hardcodeada.

No acepta argumentos por línea de comandos ni lee variables de entorno: para
cambiar los parámetros de estrategia edita los defaults de ``strategy/config.py``
y para cambiar dataset, salida o gráficos edita las constantes de
``strategy/strategy.py``.
"""

from __future__ import annotations

import sys

from strategy.strategy import main

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        print(f"Error de configuración: {exc}", file=sys.stderr)
        raise SystemExit(2)
