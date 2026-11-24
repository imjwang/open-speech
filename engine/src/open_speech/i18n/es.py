"""Mensajes en español. Las respuestas habladas se escriben para el oído: breves, sin símbolos y con cifras,
que cualquier voz en español lee bien."""

MESSAGES: dict[str, str] = {
    # ------------------------------------------------------------------ funciones que una versión puede no incluir
    "unavailable.assistant": (
        "Puedo leer en voz alta y cambiar la reproducción, las voces, las reglas y los ajustes. "
        "Las peticiones abiertas no están incluidas en esta versión."
    ),
    "unavailable.ai_cleanup": (
        "La limpieza con Claude no está incluida en esta versión, así que solo se aplican las omisiones simples de "
        "tus instrucciones de lectura."
    ),
    "unavailable.vision": "Describir imágenes no está incluido en esta versión.",
    "unavailable.speech_input": "La escucha aún no está lista en este Mac. Puedes escribir.",
    "unavailable.speech_model": "Para escucharte hace falta un modelo de voz en este Mac. Descárgalo para hablarme.",
    "unavailable.voice": "Esa voz no está incluida en esta versión. Sigo leyendo con la voz local.",
    "unavailable.rule": (
        "No puedo interpretar esa regla aquí. Prueba una más sencilla, como «omite las notas al pie» "
        "o «di SQL como sicuel»."
    ),
    "unavailable.rule_preview": "No puedo previsualizar esa regla aquí.",
    # ------------------------------------------------------------------ turnos
    "turn.empty": "Di o escribe lo que quieres que haga.",
    "turn.failed": "Algo ha fallado por mi parte. Inténtalo otra vez.",
    "agent.busy": "Claude está ocupado ahora mismo. Inténtalo en un momento.",
    "agent.offline": "Ahora no puedo conectar con Claude. La reproducción y los comandos sencillos siguen funcionando.",
    "agent.problem": "Claude ha tenido un problema con esa petición. Inténtalo otra vez.",
    "agent.refused": "Con eso no puedo ayudarte.",
    "agent.no_answer": "Lo siento, no tengo respuesta para eso.",
    # ------------------------------------------------------------------ respuestas breves
    "reply.paused": "En pausa.",
    "reply.playing": "Reproduciendo.",
    "reply.stopped": "Detenido.",
    "reply.page": "Página {page}.",
    "reply.reading": "Leyendo {title}.",
    "reply.reading_many.one": "Leyendo {n} elemento.",
    "reply.reading_many.other": "Leyendo {n} elementos, uno tras otro.",
    "reply.attachment_skipped": "He dejado fuera {name}: {reason}",
    "reply.opened": "He abierto {title}.",
    "reply.reading_clipboard": "Leyendo tu portapapeles.",
    "reply.describing_clipboard": "Describiendo tu portapapeles.",
    "reply.queue_cleared": "Cola vacía.",
    "reply.nothing_attached": "No hay nada adjunto para leer.",
    "reply.no_pronunciation": "No tengo guardada ninguna pronunciación para {term}.",
    "reply.rules_opened": "Editor de reglas abierto.",
    "reply.theme.system": "El tema sigue ahora a tu Mac.",
    "reply.theme.lavender": "Tema lavanda activado.",
    "reply.theme.ember": "Tema ámbar activado.",
    "reply.language": "De acuerdo, hablaré en español.",
    "reply.text_size": "Tamaño de letra {size}.",
    "reply.no_voice": "No he encontrado ninguna voz llamada {name}. Di «muestra las voces» para oír las opciones.",
    "reply.voice": "He cambiado a {name}.",
    "reply.voice_current": "Estás usando {name}.",
    "reply.voices_others.one": "Aquí tienes {n} más; di su nombre para escucharla.",
    "reply.voices_others.other": "Aquí tienes {n} más; di un nombre para escuchar una.",
    "reply.voices_short.one": "Aquí tienes {n} voz; di su nombre para escucharla.",
    "reply.voices_short.other": "Aquí tienes {n} voces; di un nombre para escuchar una.",
    "reply.voices_more.one": "Aquí tienes {n} voz más; di su nombre para escucharla.",
    "reply.voices_more.other": "Aquí tienes {n} voces más; di un nombre para escuchar una.",
    "reply.no_more_voices": "No hay más voces.",
    "reply.voice_sample": "Esa era {name}. Di «usa {name}» para cambiar.",
    "voices.more": "Más voces",
    "voices.more_command": "más voces",
    "reply.rules_active.one": "Hay {n} regla activa.",
    "reply.rules_active.other": "Hay {n} reglas activas.",
    "reply.recents.one": "Tienes {n} documento reciente.",
    "reply.recents.other": "Tienes {n} documentos recientes.",
    "reply.no_recents": "Aún no hay nada reciente.",
    "reply.queue.one": "Hay {n} documento en cola.",
    "reply.queue.other": "Hay {n} documentos en cola.",
    "reply.models": "Estos son los modelos locales.",
    "reply.settings": "Estos son los ajustes principales.",
    "reply.can_show": "Puedo mostrar las voces, las reglas, el gasto, los recientes, la cola, los ajustes o los "
    "modelos locales.",
    "reply.nothing_said": "Todavía no he dicho nada.",
    "reply.nothing_to_play": "No hay nada que reproducir.",
    "reply.skipped": "Saltado.",
    "reply.not_understood": "Perdona, no te he entendido.",
    "queue.empty": "No hay nada en la cola.",
    "help.basic": (
        "Puedes decir: lee mi portapapeles, pausa, más rápido, al doble, siguiente párrafo, ve a la página doce, "
        "dónde estoy, di SQL como sicuel, muestra las voces, tema oscuro, habla inglés, o cuánto he gastado."
    ),
    "help.with_agent": (
        "Puedes decir: lee mi portapapeles, pausa, más rápido, al doble, siguiente párrafo, ve a la página doce, "
        "dónde estoy, omite las notas al pie, di SQL como sicuel, muestra las voces, tema oscuro, habla inglés, "
        "o cuánto he gastado. O pídeme cualquier otra cosa, como resumir una página o escribir tus propias reglas."
    ),
    # ------------------------------------------------------------------ preguntas de confirmación (habladas)
    "ask.pronounce": "Diré {term} como {spoken}. ¿Lo guardo?",
    "ask.forget_pronunciation": "¿Olvido cómo se dice {term}?",
    "ask.budget": "¿Pongo tu presupuesto mensual en {amount}?",
    "ask.voice.default": "¿Pongo a {name} como voz predeterminada?",
    "ask.voice.assistant": "¿Pongo a {name} como voz del asistente?",
    "ask.clear_cache": (
        "Eso borra {amount} de audio guardado, así que habrá que volver a sintetizar lo que se repita. ¿Lo borro?"
    ),
    "ask.read_cost": "Leer esto costará unos {amount}, estimado. ¿Sigo?",
    "ask.save_rule": "{what}, {scope}. ¿La guardo?",
    # ------------------------------------------------------------------ títulos de tarjeta (en pantalla)
    "action.pronounce": "Decir «{term}» como «{spoken}»",
    "action.forget_pronunciation": "Olvidar «{term}»",
    "action.budget": "Presupuesto mensual · {amount}",
    "action.budget_options": "Opciones del presupuesto",
    "action.voice.default": "{name} · voz predeterminada",
    "action.voice.assistant": "{name} · voz del asistente",
    "action.clear_cache": "Borrar el audio guardado · {amount}",
    "action.read_cost": "Leer · unos {amount} (estimado)",
    "action.rule_on": "Activar · {name}",
    "action.rule_off": "Desactivar · {name}",
    "action.rule_edit": "Editar regla · {name}",
    "action.rule_delete": "Borrar regla · {name}",
    "action.preferences": "Actualizar tus instrucciones de lectura",
    "action.setting": "{key} → {value}",
    "action.install": "Instalar {name} · {size} MB",
    "action.already": "Eso ya estaba respondido.",
    # ------------------------------------------------------------------ tras confirmar
    "done.generic": "Hecho.",
    "done.cancelled": "Cancelado.",
    "done.saved": "Guardado.",
    "done.reading": "Leyendo.",
    "done.removed": "Quitado.",
    "done.rule_saved": "Guardada. La aplico desde aquí.",
    "done.rule_deleted": "Regla borrada.",
    "done.rule_updated": "Regla actualizada.",
    "done.voice": "Voz cambiada.",
    "done.budget": "Tu presupuesto mensual ahora es de {amount}.",
    "done.setting": "Ajuste cambiado.",
    "done.installing": "Instalando. Te aviso cuando esté listo.",
    "done.cache_cleared": "He borrado el audio guardado.",
    # ------------------------------------------------------------------ tarjetas y filas de ajustes
    "card.usage.today": "Gasto · hoy (estimado)",
    "card.usage.week": "Gasto · esta semana (estimado)",
    "card.usage.month": "Gasto · este mes (estimado)",
    "card.voices": "Voces",
    "card.voices_more": "Más voces",
    "card.voices_of": "Voces de {name}",
    "card.rules": "Reglas",
    "card.recents": "Recientes",
    "card.queue": "A continuación",
    "card.models": "Modelos locales",
    "card.settings": "Ajustes",
    "setting.appearance.theme": "Tema",
    "setting.language": "Idioma",
    "setting.defaults.voice": "Voz",
    "setting.defaults.speed": "Velocidad",
    "setting.reader.font_size": "Tamaño de letra",
    # ------------------------------------------------------------------ reglas
    "rules.preferences": "Instrucciones de lectura",
    "rules.pronunciations": "Pronunciaciones",
    "rules.nothing_to_save": "No hay nada que guardar.",
    "rules.invalid_file": "Ese archivo de regla no es válido: {reason}.",
    "rules.empty": "Escribe primero qué debe hacer la regla.",
    "rules.conflicts": "Entra en conflicto con: {which}. ¿Con cuál me quedo?",
    "scope.document": "este documento",
    "scope.everywhere": "en todas partes",
    "scope.for": "para {what}",
    "scope.for_document": "para este documento",
    "scope.for_documents": "para estos {n} documentos",
    "scope.for_languages": "para documentos en {what}",
    "type.pdf": "los PDF",
    "type.web": "las páginas web",
    "type.docx": "los documentos de Word",
    "type.text": "el texto",
    "type.image": "las imágenes",
    "type.audio": "el audio",
    "list.and": " y ",
    # ------------------------------------------------------------------ lo que cambia una regla propuesta
    "diff.on_these_pages": "En estas páginas",
    "diff.on_this_page": "En esta página",
    "diff.in_this_document": "En este documento",
    "diff.removes": "quita {what}",
    "diff.changes": "cambia {what}",
    "diff.rewrites": "reescribe {what}",
    "diff.brings_back": "recupera {what}",
    "diff.no_change": "{where} no cambia nada. ¿La guardo {scope} de todos modos?",
    "diff.summary": "{where} eso {what}. ¿La guardo {scope}?",
    "noun.heading.one": "título",
    "noun.heading.other": "títulos",
    "noun.paragraph.one": "párrafo",
    "noun.paragraph.other": "párrafos",
    "noun.list_item.one": "elemento de lista",
    "noun.list_item.other": "elementos de lista",
    "noun.quote.one": "cita textual",
    "noun.quote.other": "citas textuales",
    "noun.code_block.one": "bloque de código",
    "noun.code_block.other": "bloques de código",
    "noun.table.one": "tabla",
    "noun.table.other": "tablas",
    "noun.caption.one": "leyenda",
    "noun.caption.other": "leyendas",
    "noun.figure.one": "figura",
    "noun.figure.other": "figuras",
    "noun.footnote.one": "nota al pie",
    "noun.footnote.other": "notas al pie",
    "noun.header.one": "encabezado",
    "noun.header.other": "encabezados",
    "noun.footer.one": "pie de página",
    "noun.footer.other": "pies de página",
    "noun.page_number.one": "número de página",
    "noun.page_number.other": "números de página",
    "noun.equation.one": "ecuación",
    "noun.equation.other": "ecuaciones",
    "noun.table_of_contents_entry.one": "entrada del índice",
    "noun.table_of_contents_entry.other": "entradas del índice",
    "noun.reference.one": "referencia",
    "noun.reference.other": "referencias",
    "noun.citation.one": "cita",
    "noun.citation.other": "citas",
    "noun.link.one": "enlace",
    "noun.link.other": "enlaces",
    "noun.url.one": "URL",
    "noun.url.other": "URL",
    "noun.emphasized_phrase.one": "frase destacada",
    "noun.emphasized_phrase.other": "frases destacadas",
    "noun.parenthetical.one": "paréntesis",
    "noun.parenthetical.other": "paréntesis",
    "noun.bracketed_aside.one": "inciso entre corchetes",
    "noun.bracketed_aside.other": "incisos entre corchetes",
    "noun.braced_span.one": "tramo entre llaves",
    "noun.braced_span.other": "tramos entre llaves",
    "noun.formula.one": "fórmula",
    "noun.formula.other": "fórmulas",
    "noun.footnote_marker.one": "llamada de nota",
    "noun.footnote_marker.other": "llamadas de nota",
    "noun.passage.one": "pasaje",
    "noun.passage.other": "pasajes",
    "noun.other_passage.one": "pasaje más",
    "noun.other_passage.other": "pasajes más",
    "noun.phrase.one": "frase",
    "noun.phrase.other": "frases",
    "noun.section.one": "sección",
    "noun.section.other": "secciones",
    # ------------------------------------------------------------------ dónde estoy
    "where.nothing": "Ahora no se está reproduciendo nada.",
    "where.this_document": "este documento",
    "where.page": "Página {page} de {pages},",
    "where.percent": "vas por el {n} por ciento,",
    "where.left": "quedan unos {time} a {speed}.",
    "where.left_soon": "queda {time} a {speed}.",
    # ------------------------------------------------------------------ cifras para el oído
    "money.nothing": "nada",
    "money.under_a_cent": "menos de un centavo",
    "money.cents.one": "{words} centavo",
    "money.cents.other": "{words} centavos",
    "money.dollars.one": "{words} dólar",
    "money.dollars.other": "{words} dólares",
    "money.dollars_cents.one": "{dollars} dólar con {cents} centavos",
    "money.dollars_cents.other": "{dollars} dólares con {cents} centavos",
    "fraction.quarter": "una cuarta parte",
    "fraction.third": "un tercio",
    "fraction.half": "la mitad",
    "fraction.two_thirds": "dos tercios",
    "fraction.three_quarters": "tres cuartas partes",
    "fraction.all": "todo",
    "fraction.percent": "cerca del {n} por ciento",
    "duration.hm": "{h} y {m}",
    "duration.hours.one": "{n} hora",
    "duration.hours.other": "{n} horas",
    "duration.minutes.one": "{n} minuto",
    "duration.minutes.other": "{n} minutos",
    "duration.under_a_minute": "menos de un minuto",
    "size.gb": "unos {n} gigas",
    "size.mb": "unos {n} megas",
    # ------------------------------------------------------------------ gasto
    "period.today": "hoy",
    "period.week": "esta semana",
    "period.month": "este mes",
    "usage.nothing": "No has gastado nada {when}.",
    "usage.under_a_cent": "Menos de un centavo {when}, estimado",
    "usage.total": "Unos {amount} {when}, estimado",
    "usage.mostly": ", sobre todo en {what}",
    "usage.all": ", todo en {what}",
    "usage.provider_op": "{op} de {provider}",
    "usage.budget_reached": "Has llegado a tu presupuesto de {budget}.",
    "usage.budget_well_under": "Vas muy por debajo de tu presupuesto de {budget}.",
    "usage.budget_at": "Llevas {fraction} de tu presupuesto de {budget}.",
    "usage.cache_saved": "El audio guardado te ha ahorrado unos {amount}.",
    "op.tts": "voz",
    "op.preview": "muestras de voz",
    "op.stt": "transcripción",
    "op.annotate": "limpieza",
    "op.transform": "resúmenes",
    "op.compile": "redacción de reglas",
    "op.vision": "descripciones de imágenes",
    "op.chat": "el asistente",
    "budget.warn": (
        "Aviso: has usado cerca del {pct} por ciento de tu presupuesto de {budget} este mes (unos {spent}, estimado)."
    ),
    "budget.stop": (
        "Has llegado a tu presupuesto mensual de {budget}. Sigo leyendo con la voz local; "
        "di «sube mi presupuesto» para cambiarlo."
    ),
    "budget.passed": "Has superado tu presupuesto mensual de {budget} (unos {spent}, estimado).",
    "notice.local_voice": "la voz local",
    "notice.another_voice": "otra voz",
    "notice.budget": "Presupuesto agotado; leo con {target}.",
    "notice.offline": "No he podido conectar con {name}; leo con {target}.",
    "notice.unavailable": "Esa voz no está disponible ahora; leo con {target}.",
    "notice.not_responding": "{name} no responde; leo con {target}.",
    "notice.free_tier": "Se ha acabado la voz de Google gratuita de este mes; leo con {target}.",
    # ------------------------------------------------------------------ las voces de Google y sus caracteres gratis
    "google.blocked": (
        "Las voces de Google necesitan Cloud Text-to-Speech activado para la credencial de esta versión."
    ),
    "google.disabled": (
        "Las voces de Google necesitan Cloud Text-to-Speech activado en el proyecto de Google Cloud de esta versión."
    ),
    "google.billing": (
        "Las voces de Google necesitan la facturación activada en el proyecto de Google Cloud de esta versión."
    ),
    "google.restricted": "La credencial de Google de esta versión no puede usar Cloud Text-to-Speech desde este Mac.",
    "free_tier.label.google": "Voz de Google",
    "free_tier.exhausted": "Se han acabado los caracteres gratis de la voz de Google de este mes.",
    "free_tier.warn": "Aviso: has usado cerca del {pct} por ciento de la voz de Google gratuita de este mes, estimado.",
    "free_tier.stop": (
        "Se ha acabado la voz de Google gratuita de este mes, así que leo con la voz local hasta el mes que viene. "
        "Di «sigue con la voz de Google» para mantenerla, por unos {rate} cada mil caracteres."
    ),
    "free_tier.passed": (
        "Se ha acabado la voz de Google gratuita de este mes; ahora cuesta unos {rate} cada mil caracteres, estimado."
    ),
    "voice.auto": "Automática ({name})",
    "reply.google_unavailable": (
        "Las voces de Google no están disponibles en esta versión, así que leo con la voz local."
    ),
    "reply.beyond_free_tier.already": "Ya mantienes la voz de Google este mes.",
    "ask.beyond_free_tier": (
        "¿Mantengo la voz de Google cuando se acaben los caracteres gratis de este mes? Cuesta unos {rate} cada mil "
        "caracteres, estimado, y tu presupuesto sigue valiendo. ¿La mantengo?"
    ),
    "action.beyond_free_tier": "Mantener la voz de Google este mes · {rate} por 1.000 caracteres",
    "done.beyond_free_tier": "Vale, mantengo la voz de Google hasta final de mes.",
    # ------------------------------------------------------------------ errores que la gente se encuentra
    "what.action": "esa acción",
    "what.document": "ese documento",
    "what.voice": "esa voz",
    "what.recording": "esa grabación",
    "what.file": "el archivo {name}",
    "error.not_found": "No he encontrado {what}.",
    "error.nothing_to_read": "Con las reglas actuales no hay nada que leer en ese documento.",
    "error.no_page": "Este documento no tiene página {page}.",
    "error.nothing_to_play": "Aún no hay nada que reproducir. Dime qué leer o suelta un archivo en la barra.",
    "error.nothing_playing": "No se está reproduciendo nada.",
    "error.nothing_open": "No hay nada abierto. Dime primero qué leer.",
    "error.no_text": "No hay texto que leer.",
    "error.clipboard_empty": "Tu portapapeles está vacío. Copia antes un texto o una imagen.",
    "error.unsupported_file": "Todavía no puedo leer archivos {ext}. Funcionan PDF, DOCX, texto, Markdown e imágenes.",
    "error.pronunciation_incomplete": "Hacen falta la palabra y cómo se dice.",
    "error.budget_exceeded": (
        "Has llegado a tu presupuesto mensual de {budget} dólares. Sigo leyendo con la voz local; "
        "di «sube mi presupuesto» para cambiarlo."
    ),
    "error.estimate_required": "Eso costará unos {usd} dólares. Confirma para continuar.",
    "error.estimate_required.read": (
        "Leer esto costará unos {usd} dólares (estimado). Pulsa reproducir otra vez para continuar."
    ),
    "alert.read_cost": "Leer {title} costará unos {amount} (estimado). Pulsa reproducir para continuar.",
    "page.bad_url": "Ese enlace no lo puedo abrir.",
    "page.private": "No puedo abrir {host}: está en este ordenador o en tu red local.",
    "page.unreachable": "No he podido conectar con {host}.",
    "page.http_error": "Esa página ha devuelto un error ({status}).",
    "page.too_large": "Esa página es demasiado grande para leerla.",
    "page.redirects": "Ese enlace redirige demasiadas veces.",
    # ------------------------------------------------------------------ notas sobre cómo se preparó un documento
    "plan.ai_paused": "La limpieza se ha pausado: {detail}",
    "plan.budget_filters_only": (
        "Has agotado tu presupuesto, así que Claude no ha limpiado este texto: solo se aplican las omisiones "
        "simples de tus instrucciones de lectura."
    ),
    "plan.ops_dropped.one": "He descartado {n} cambio de la IA que no coincidía exactamente con el texto.",
    "plan.ops_dropped.other": "He descartado {n} cambios de la IA que no coincidían exactamente con el texto.",
    "plan.ops_refused.one": (
        "He ignorado {n} cambio de la IA que habría quitado más de lo que cubre la regla; ese texto se lee tal cual."
    ),
    "plan.ops_refused.other": (
        "He ignorado {n} cambios de la IA que habrían quitado más de lo que cubre la regla; ese texto se lee tal cual."
    ),
    "plan.rule_unavailable": "«{name}» necesita Claude, así que se lee el texto original.",
    "plan.rule_paused": "«{name}» se ha pausado: {detail}",
    "plan.rule_unusable": "«{name}» ha devuelto algo inservible en una sección; se lee tal cual.",
    "plan.rule_needs_ai": "«{name}» necesita Claude, así que no se ha aplicado.",
    "plan.one_rewrite": "Solo se aplica una regla de reescritura a la vez; «{name}» no se ha aplicado.",
    "plan.scanned": "La página {pages} no tiene capa de texto (está escaneada), así que no se puede leer sin "
    "extracción con IA.",
    "notice.no_style": "Esta voz no admite instrucciones de estilo, así que leo sin ellas.",
    # ------------------------------------------------------------------ voces Qwen3-TTS locales (providers/qwen3.py)
    "qwen.voice_name": "{speaker} (Qwen, {language})",
    "qwen.clone_name": "{name} (voz clonada, Qwen)",
    "qwen.lang.en": "inglés",
    "qwen.lang.es": "español",
    "qwen.lang.zh": "chino",
    "qwen.lang.ja": "japonés",
    "qwen.lang.ko": "coreano",
    "qwen.lang.de": "alemán",
    "qwen.lang.fr": "francés",
    "qwen.lang.ru": "ruso",
    "qwen.lang.pt": "portugués",
    "qwen.lang.it": "italiano",
    "qwen.variant": "{size} {precision}",
    "qwen.unavailable.chip": "Las voces Qwen necesitan un Mac con Apple Silicon.",
    "qwen.unavailable.runtime": "El motor de las voces Qwen no viene en esta versión.",
    "qwen.unavailable.download": "Descarga las voces Qwen ({variant}, unos {size}) para usarlas.",
    "qwen.unavailable.download_clone": (
        "Las voces clonadas necesitan el modelo de clonación de Qwen ({variant}, unos {size})."
        " Descárgalo para usarlas."
    ),
    "qwen.unavailable.memory": (
        "Este tamaño de Qwen necesita unos {ram} GB de memoria y este Mac tiene {mem} GB."
        " Elige el tamaño 0.6B."
    ),
    "qwen.unavailable.clone_missing": "Falta la grabación de esta voz clonada. Grábala de nuevo.",
    "qwen.unavailable.unknown_clone": "No hay ninguna voz clonada llamada {name}.",
    "qwen.note.sibling": "Uso las voces Qwen {have} descargadas, porque {want} no está descargada.",
    "qwen.error.load": "No pude cargar la voz Qwen. Quítala y descárgala de nuevo.",
    "qwen.error.load_timeout": "La voz Qwen tardó demasiado en cargarse. Inténtalo de nuevo o elige un tamaño menor.",
    "qwen.error.load_crashed": (
        "La voz Qwen se detuvo mientras se cargaba, quizá por falta de memoria."
        " Cierra otras apps o elige un tamaño menor."
    ),
    "qwen.error.load_start": "No se pudo iniciar el motor de la voz Qwen.",
    "qwen.error.crashed": "La voz Qwen se detuvo de forma inesperada.",
    "qwen.error.failed": "La voz Qwen falló: {detail}",
    "notice.slow_voice": (
        "Esta voz no da abasto a {speed} veces la velocidad en este Mac."
        " Un tamaño más pequeño o Kokoro sí."
    ),
    "notice.slow_voice_small": "Esta voz no da abasto a {speed} veces la velocidad en este Mac. Kokoro sí.",
    "qwen.voices_spoken": "las voces Qwen {size}",
    "qwen.size.small": "pequeñas",
    "qwen.size.large": "grandes",
    "ask.install_qwen": "Descargar {name} ocupa {size}. ¿Las descargo ahora?",
    "reply.qwen_installed": "{name} ya están en este Mac.",
    "models.row": "{size} · {ram} GB de memoria",
    # ------------------------------------------------------------------ voces clonadas y diseñadas (service/clones.py)
    "qwen.design_name": "{name} (voz diseñada, Qwen)",
    "qwen.unavailable.download_design": (
        "Para diseñar voces hace falta el modelo de diseño de voz de Qwen ({variant}, unos {size})."
        " Descárgalo para usarlo."
    ),
    "what.clone": "esa voz",
    "what.audio_file": "ese archivo de audio",
    "what.candidate": "esa muestra de voz",
    "clone.default_name": "Mi voz",
    "clone.consent": (
        "Esta es mi voz, o tengo permiso de la persona para clonarla. Las voces clonadas se quedan en este Mac."
    ),
    "clone.consent_required": (
        "Para clonar hace falta confirmar que es tu voz, o que tienes permiso de la persona."
    ),
    "clone.unknown_passage": "No conozco ese texto. Vuelve a pedir los textos.",
    "clone.no_audio": "Envía una grabación o un archivo de audio para hacer una voz.",
    "clone.job": "Creando la voz",
    "clone.step.cleaning": "Limpiando la grabación",
    "clone.step.listening": "Comprobando lo que se dijo",
    "clone.step.saving": "Guardando la voz",
    "clone.step.checking": "Probando la voz nueva",
    "clone.step.done": "La voz está lista",
    "clone.step.downloading": "Descargando el modelo de clonación, {pct} %",
    "clone.working": "Entendido. Un momento mientras creo la voz.",
    "clone.check.needs_download": "Podré probarla cuando se descargue el modelo de clonación, de {size}.",
    "clone.reject.mismatch": (
        "Esa grabación no coincide con el texto. Léelo tal como está, a tu ritmo normal, y vuelve a intentarlo."
    ),
    "clone.reject.need_text": "En este Mac no puedo saber qué dice esta grabación. Escribe lo que se dice en ella.",
    "clone.reject.no_words": "No he entendido ninguna palabra en esa grabación.",
    "clone.reject.empty_name": "Una voz necesita un nombre.",
    "clone.reject.low_rate": "Esa grabación tiene calidad de teléfono y la voz sonaría apagada. Usa una mejor.",
    "clone.reject.no_speech": "No he oído voz en esa grabación.",
    "clone.reject.too_short": (
        "Necesito al menos {needed} segundos de voz y esa grabación tiene {seconds}."
        " Lee el texto entero o usa una grabación más larga."
    ),
    "clone.reject.clipped": (
        "Esa grabación está distorsionada porque sonaba demasiado fuerte. Aléjate un poco del micrófono y repite."
    ),
    "clone.reject.noisy": "Hay demasiado ruido de fondo en esa grabación. Prueba en un sitio más tranquilo.",
    "clone.reject.unreadable": "No puedo abrir {name} como audio.",
    "clone.reject.unusable": "No puedo usar esa grabación.",
    "clone.warn.clipped": "Algunos picos fuertes están recortados.",
    "clone.warn.noisy": "Hay algo de ruido de fondo y la voz puede llevarlo.",
    "clone.warn.low_rate": "La grabación es de baja calidad y la voz puede sonar apagada.",
    "clone.warn.quiet": "La grabación es muy baja.",
    "clone.warn.long": "He usado {seconds} segundos de una frase larga.",
    "clone.warn.unverified": "En este Mac no he podido comprobar la grabación con el texto.",
    "ask.clone_download": (
        "Para clonar una voz necesito antes un modelo de {size}. Se queda en este Mac. ¿Lo descargo?"
    ),
    "action.clone_download": "Descargar el modelo de clonación · {size}",
    "ask.clone_record": "Lee en voz alta el texto de la pantalla, con tu voz normal. Di listo, o pulsa grabar.",
    "action.clone_record": "Clonar tu voz",
    "reply.clone_no_audio": "Adjunta o arrastra primero un archivo de audio y luego di «clona esta voz».",
    "ask.clone_file": (
        "Voy a crear una voz a partir de {name}. Clona solo una voz que sea tuya o que tengas permiso para usar."
        " ¿Sigo?"
    ),
    "ask.clone_file_download": "Antes se descarga el modelo de clonación, de {size}.",
    "action.clone_file": "Clonar la voz de {name}",
    "ask.design_download": (
        "Para diseñar voces hacen falta antes {size} de modelos. Se quedan en este Mac. ¿Los descargo?"
    ),
    "action.design_download": "Descargar los modelos de voz · {size}",
    "action.design": "Crear tres voces: {description}",
    "action.download_size": "descarga de {size}",
    "reply.no_clone": "No tienes ninguna voz llamada {name}.",
    "ask.delete_voice": "¿Borro la voz {name}? No se puede deshacer.",
    "action.delete_voice": "Borrar «{name}»",
    "reply.clone_renamed": "He cambiado el nombre de {old} a {new}.",
    "done.clone_go": "Adelante.",
    "done.clone_downloading": "Descargando el modelo de clonación. Puedes grabar mientras se descarga.",
    "done.clone_made": "He creado la voz {name}.",
    "done.clone_kept": "Guardada. A partir de ahora leeré con ella.",
    "done.clone_discarded": "Vale, la he borrado.",
    "done.voice_deleted": "He borrado {name}.",
    "reply.clone_saved_unchecked": "He guardado {name}. {note}",
    "ask.keep_clone": "¿La guardo?",
    "ask.keep_clone_fair": "Suena un poco áspera. ¿La guardo de todos modos?",
    "ask.keep_clone_poor": (
        "No ha salido clara. Un sitio más tranquilo o una grabación más limpia ayudan. ¿La guardo de todos modos?"
    ),
    "action.keep_clone": "Guardar «{name}»",
    "design.job": "Diseñando voces",
    "design.step": "Creando la voz {n} de {total}",
    "design.done": "Las voces están listas",
    "design.working": "Creando tres voces. Tarda como medio minuto.",
    "design.reject.describe": "Dime cómo debe sonar la voz, por ejemplo «una voz cálida y grave, tranquila y lenta».",
    "design.reject.failed": "No he podido crear una voz con esa descripción. Prueba a describirla de otra forma.",
    "design.reject.download": (
        "Para diseñar voces hace falta el modelo de diseño de voz, de unos {size}. Descárgalo antes."
    ),
    "design.candidate": "Voz {n}",
    "design.say_candidate": "Voz {n}.",
    "design.keep_command": "quédate con la voz {n}",
    "design.shown": "Aquí tienes {n} voces.",
    "card.design": "Voces para «{name}»",
    "ask.design_which": "¿Cuál guardo? Di su número, o ninguna.",
    "action.keep_design": "Elegir una voz para {name}",
    "done.design_kept": "He guardado «{name}». A partir de ahora leeré con ella.",
    "done.design_discarded": "Vale, ninguna.",
    "voices.clone": "Clonar una voz",
    "voices.clone_command": "clona mi voz",
    "voices.delete_command": "borra la voz {name}",
    "setting.qwen3.size": "Tamaño de la voz local",
    "setting.qwen3.precision": "Precisión de la voz local",
    "models.family.customvoice": "Voces Qwen",
    "models.family.base": "Clonación de voz Qwen",
    "models.family.voicedesign": "Diseño de voz Qwen",
}
