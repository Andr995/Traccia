# Alternative: funzionamento e limiti

`POST /api/analyze` restituisce il dossier e le alternative curate disponibili,
con `alternatives.pending: true`. L'interfaccia avvia poi `POST /api/alternatives`
con `company`, `entity_id` facoltativo e `country_code`. Il report resta leggibile
mentre Wikidata risponde. Gli altri client API devono effettuare la stessa seconda
richiesta per ottenere i candidati automatici.

La ricerca distingue marchi, prodotti e imprese. I nomi sono normalizzati senza
perdere gli alfabeti non latini; una corrispondenza ambigua tra imprese richiede
la selezione esplicita dell'identificativo Wikidata. Per un marchio senza settore,
il sistema può seguire al massimo due collegamenti a produttori/proprietari.
La scheda usata per il confronto viene citata separatamente dall'identità del dossier.

Le imprese candidate devono avere settore, paese e sito associati su Wikidata.
Se è disponibile una categoria di prodotto, viene applicato anche questo filtro;
un prodotto specifico non eredita tutte le categorie del suo produttore. Per il
catalogo Coca-Cola il confronto è limitato alle cole. BM25 ordina poi nomi e
descrizioni: il suo punteggio non è una probabilità di equivalenza o affidabilità.

Le fonti di segnalazione controllano il nome del candidato e i nomi di proprietari
diretti disponibili. Una corrispondenza esclude il candidato dai suggerimenti,
ma non costituisce un verdetto di relazione. Mancanza di segnalazioni, sede nel
paese e sito associato non provano rispettivamente assenza di legami, disponibilità
nei negozi o attualità del sito. Proprietari non indicati e catene societarie oltre
il primo livello restano non verificati. I paesi supportati sono IT, FR, DE, ES,
GB e US: non esiste una promessa di copertura di tutte le aziende.

I download hanno limiti di dimensione e timeout. Le risposte valide sono conservate
in cache per sei ore; i fallimenti transitori per un minuto. La cache è in memoria
e si perde al riavvio. Una richiesta di alternative per volta limita il carico
delle fonti; in caso di servizio occupato la UI consente di riprovare. Il modulo
automatico usa solo la libreria standard Python, senza modelli aggiuntivi.

Le conferme primarie del dossier raggruppano le dichiarazioni per autore: filing
SEC e pagina aziendale della stessa società contano una volta. I controlli su
negazioni e soggetto restano euristiche, non una verifica semantica completa.

## Verifiche locali

Con Flask installato nell'ambiente Python:

```text
python -m unittest discover -s tests -v
npm ci
npm test
```

I test Python sono offline e usano fonti simulate. I test JavaScript usano jsdom
per controllare richieste tardive, cambi di paese, errori delle API, cronologia e
caricamento separato delle alternative. Node e jsdom sono strumenti di sviluppo:
il servizio Render continua a usare Python e `requirements.txt`.
Questi controlli non misurano il consumo massimo dei modelli ONNX/spaCy su Render,
né garantiscono l'accessibilità futura delle fonti esterne.

Riferimenti sul modello dati: [accesso ai dati Wikidata](https://www.wikidata.org/wiki/Help:Data_access),
[produttore P176](https://www.wikidata.org/wiki/Property:P176),
[proprietario P127](https://www.wikidata.org/wiki/Property:P127),
[categoria cola](https://www.wikidata.org/wiki/Q134041).
