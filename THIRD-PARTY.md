# Зависимости и лицензии

В архиве находятся исходники приложения, но не копии библиотек. Они устанавливаются pip отдельно. Перед распространением самодостаточного EXE нужно включить полные тексты лицензий зависимостей.

| Пакет | Версия | License-Expression / License из метаданных установленного пакета |
| --- | --- | --- |
| fastapi | 0.115.12 | Не указано; проверить дистрибутив |
| uvicorn | 0.34.2 | BSD-3-Clause |
| starlette | 0.46.2 | BSD-3-Clause |
| pydantic | 2.14.3 | MIT |
| pydantic-core | 2.50.4 | MIT |
| typing-extensions | 4.16.0 | PSF-2.0 |
| annotated-types | 0.8.0 | MIT |
| typing-inspection | 0.4.5 | MIT |
| anyio | 4.16.0 | MIT |
| idna | 3.20 | BSD-3-Clause |
| h11 | 0.16.0 | MIT |
| click | 8.5.1 | BSD-3-Clause |
| httpx | 0.28.1 | BSD-3-Clause |
| httpcore | 1.0.9 | BSD-3-Clause |
| certifi | 2026.10.19 | MPL-2.0 |
| pypdf | 7.1.1 | BSD-3-Clause |
| python-docx | 1.2.0 | MIT |
| lxml | 6.1.3 | BSD-3-Clause |
| tzdata | 2026.6 | Apache-2.0 |
| docxtpl | 0.20.2 | LGPL-2.1-only |
| Jinja2 | 3.1.6 | BSD-3-Clause (LICENSE.txt в дистрибутиве) |
| MarkupSafe | 3.0.3 | BSD-3-Clause |

На Windows дополнительно устанавливается colorama 0.4.6. Его полный текст лицензии берётся из устанавливаемого дистрибутива.

Python, браузер и инструменты тестирования устанавливаются отдельно и не входят в архив сборки.

M4 использует установленный в Windows PowerShell/.NET/WinForms и системную ассоциацию DOCX; их дистрибутивы в архив не включаются. Установка LibreOffice не выполняется.
