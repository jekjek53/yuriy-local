# Локальный API M4 · 0.6.0-m4

Дополнение к `API.md`. Та же граница Host/Origin, текущий X-Yuriy-Token, без удалённого доступа. ID берутся из ответов службы. Примеры — структуры, не готовые реальные ID. Не направляйте тестовые запросы на рабочие проекты без копии.

## Секретарь и настройки

- `GET /api/secretary` → `{settings,items,outstanding,total,lastTick,schedulerError,serviceRequired:true}`. items до 500: сначала актуальные непросмотренные. Не инициирует платный вызов.
- `POST /api/secretary/settings` → `{timezone:"Europe/Samara",notificationsEnabled:false,confirmed:true}`. Подтверждает IANA-зону и разрешение системного показа. Не переносит существующие события. Отдельный переключатель уведомлений не включает автозапуск Windows.
- `POST /api/secretary/acknowledge` → `{id:"ID квитанции из items"}`. Это явная отметка просмотра, не подтверждение факта показа ОС.

Настройки и квитанции — meta `secretary-v1` основной SQLite, входят в согласованный backup. Изменение квитанций не увеличивает общую UI-ревизию; фоновый цикл не создаёт ложные конфликты окон. Новых таблиц/версии схемы в M4 нет.

## Предложение события

`POST /api/projects/{pid}/event-suggestion` с `{source:{…}}`:

- `{kind:"text",text:"Завтра заседание в 14:00"}` — локальное поручение до 5000 символов;
- `{kind:"message",messageId:"…",text:"точный фрагмент до 5000"}` — проверяется наличие сообщения в проекте и вхождение, сохраняется SHA сообщения;
- `{kind:"document",fileId:"…",offset:0,text:"точный фрагмент"}` — offset порядкового chunk, материал должен быть извлечён/актуален; сервер проверяет вхождение, сохраняет revisionId/SHA/координаты.

Ответ: предложенные title/date/time/timezone/allDay/type, warnings, proceduralDraft, referenceDate/referenceInstant, source, dateCandidates, weekday, needsTimeChoice, method=local-dates-weekdays-v2, externalApiCalled=false. Никакой записи события, сетевого вызова модели, вычисления процессуального срока. Неопределённая дата остаётся пустой.

## Карточка события

`POST /api/projects/{pid}/event-plan`:

```json
{
  "revision":42,
  "eventId":"ID существующего события при редактировании; иначе не передавать",
  "source":{"kind":"manual"},
  "event":{
    "title":"Заседание",
    "date":"2030-06-14",
    "time":"10:00",
    "timezone":"Europe/Samara",
    "allDay":false,
    "type":"hearing",
    "status":"planned",
    "note":"",
    "place":"",
    "caseNumber":"",
    "endDate":"",
    "endTime":"",
    "fold":null,
    "endFold":null,
    "reminders":[1440,60,0],
    "reminderTime":""
  }
}
```

В реальном новом запросе eventId опустите, не посылайте пояснение из примера как ID. Для предложения source задаётся как выше. При редактировании происхождение уже подтверждённого события сохраняется сервером. Дополнительно возможен proceduralDraft:true; тип deadline/явные слова о сроке также включают предупреждение независимо от этого флага.

Типы: hearing/letter/meeting/deadline/response/task/other. Статусы planned/completed/cancelled. Для «весь день» time="", allDay=true, startsAt=null; при непустом reminders обязательна явная reminderTime. Смещения целые 0–43200 минут, разные, до 5. UTC формирует сервер через IANA. Для неоднозначного времени нужен fold=0|1, для несуществующего — отказ. Окончание временного события строго позже начала.

Ответ — planId, kind=event, projectId/projectName, revision, before, event, warning, expiresInSeconds=600. Поле confirmed внутри кандидата — будущая запись после подтверждения, не факт существующего события. До confirm state не меняется. Максимум 10 карточек в памяти. Даты/время/источник нельзя подменить отдельным телом confirm.

## Архив

`POST /api/projects/{pid}/archive-plan` → `{revision,archived:true|false,eventsChoice:"keep"|"cancel"}`. Ответ содержит futureIds/futureEvents и конкретную политику. keep оставляет будущие события действующими; cancel при архивировании помечает их отменёнными (старые legacy — done). Возвращение проекта само не возобновляет отменённые события.

Новые управляемые события и квитанция архива защищены от подмены обычным PUT state. Архивирование проекта с действующими M4-событиями через PUT отклоняется: нужен явный выбор. Прежний API legacy-событий сохранён для совместимости, но не может включить им напоминания без карточки M4.

## Подтверждение/отмена карточки

- `POST /api/secretary/plans/{planId}/confirm` с `{confirmed:true}` → `{revision,confirmationId,eventId}`.
- `POST /api/secretary/plans/{planId}/discard` — забыть карточку.

Одноразовый план, срок 10 минут, обязательная актуальная ревизия и доступность проекта. Карточки аннулируются после перезапуска/restore. При потере HTTP-ответа сверяйте `event.confirmationId` либо `project.archiveConfirmationId` в GET state; не создавайте новый дубликат вслепую. UI выполняет сверку.

## Расписание и центр

Каждое подтверждение меняет sequence. **scheduleVersion** меняется только для полей расписания/статуса, а не названия/заметки. Ключ квитанции: eventId:scheduleVersion:offset. Изменение времени/зоны/смещений отменяет прежнее расписание; просмотренные записи не переотправляются.

Квитанция хранит dueAt в UTC, eventId/projectId, дату/время/зону, missed, acknowledged, superseded, notification. notification: not-requested, attempt-recorded, unavailable, failed, requested-not-confirmed. Последнее означает только завершение запроса механизму ОС, не показ/прочтение.

Фоновая проверка примерно каждые 5 секунд, первый запуск или перерыв больше 45 секунд → только пропущенные в центре. Попытка показа фиксируется до вызова ОС; авария между записью и вызовом не ведёт к автоматическому повтору. Запись ранее отменённого расписания superseded не относится к текущему outstanding. Центр не является журналом подтверждённой доставки человеку.

## Версии

- `GET /api/projects/{pid}/operations/{operationId}/revision-preview?which=before|after&offset=0` → `{name,sha256,chars,chunks,offset,hasMore,warnings}`. Извлечение в ограниченном worker, до 20 фрагментов за запрос; сам индекс текущего материала не меняется. Не визуальный Word-preview.
- `POST /api/projects/{pid}/operations/{operationId}/restore-plan` с `{which:"before"|"after"}` → обычный edit-план M3b. Скачивание/подтверждение/отмена — прежние `/api/operations/{planId}/…`.

Выбранная редакция должна относиться к цепочке с единственным текущим прикреплённым материалом. Кандидат — точные сохранённые байты, проверенные по SHA. После подтверждения новый fileId, mandatory backup и новая запись operations с restoredFromFileId; старые ID/файлы не перезаписываются.

## Передача DOCX редактору

- `POST /api/projects/{pid}/results/{rid}/open-editor`
- `POST /api/projects/{pid}/documents/{fid}/open-editor`

Тело `{confirmed:true,sha256:"SHA показанного DOCX"}`. Сервер сверяет источник/тип/хеш, копирует в новое имя .docx внутри temporary и только затем вызывает Windows-ассоциацию. Импортированный DOCX проходит ограниченный preflight контейнера; это не антивирус. Произвольные программы/пути/аргументы API не принимает.

Ответ `{requested,reason,downloadAvailable:true}` и при запросе copyPath. requested=true **не означает**, что редактор открылся или что документ напечатан. Вне локальной Windows/при preview возвращается честный запасной сценарий. При отсутствии ассоциации/ошибке — сохранение файла. Копии temporary не входят в backup и не становятся редакциями проекта автоматически.

TXT/HTML-печатное представление и `window.print()` выполняются браузером после явного предпросмотра/нажатия. HTML экранируется как текст; факта «распечатано» API не создаёт.


## Обычный чат → секретарь (0.6.0)

`POST /api/chat/route` с `{text}` → `{local:boolean}`: локальная проверка маршрута, без записи и платного API. Не заменяет проверку действительного `/api/chat`.

`POST /api/chat` сохраняет прежний контракт requestId/projectId/threadId/revision/text/deep/documentIds/documentsConsent. Распознанная прямая команда идёт локально, даже если ключ не задан. Возвращается requestId; прежний SSE endpoint сразу отдаёт сохранённый завершённый ответ `kind=secretaryProposal`, `tier=local`, `contextSummary.externalApiCalled=false`. Если зона подтверждена, `contextSummary.eventProposal` — снимок с referenceDate/referenceInstant/sourceArgs. Иначе там null и сообщение о необходимости подтвердить зону. События создаются только через существующий event-plan и отдельное подтверждение.

Старые сообщения/события не изменяются. Произвольные вопросы не превращаются в события. Дни недели без явно определённой недели дают пустую дату и кандидатов; несколько времён и отсутствие времени требуют явного выбора, не скрытого allDay.
