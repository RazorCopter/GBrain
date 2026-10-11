# ADR 0001: Decisioni di base sullo Stack e sul Modello del Pattern di Progettazione

## Obiettivo
Dispiegare un sistema performante per uso personale, autonomo, su base hardware economica (Linux x86_64 precluso da GPU) focalizzandosi sull'estrazione mnemonica senza compromettere le informazioni con software sperimentali proni alla perdita in update aggressivi o librerie volatili.

## Esclusioni
1. Eliminati KafKa, Redis per evitare over-head ingegneristico e ram footprint alto, non sostenibile.
2. Mem0 scartato per obblighi dipendentistici di base o requisiti incompatibili collaterali se non incapsulabile in adattatore debole.

## Decisione Adozione:
- **Backend Centrale Assoluto:** Python con FastAPI (veloce) e SQLAlchemy.
- **Job Asincroni e Task Queue:** Procrastinate (usa native postgresql NOTIFY / LISTEN), integrato elegantemente nell'ORM, massimizzando il fattore "Meno Processi Demone è Meglio" ed evitando cache state-loss.
- **Coerenza del Dato:** Si opta per l'**Outbox Pattern** unito ad **Eventual Consistency** verso il motore a Grafo (Neo4j). PostgreSQL deve restare l'autorità necessaria a una futura ricostruzione deterministica del grafo. Nella fondazione Fase 1 questa è soltanto un'invariante architetturale: sink, modello semantico e comando di rebuild non sono implementati, quindi `pg_dump` da solo non costituisce ancora un backup completo e il set operativo include anche un dump Neo4j.
- **Erogazione Fronte-Rete:** React / TypeScript interamente confinato a chiamate verso le nostre API FastAPI; verrà servito o pre-compilato come static object su root path di FastAPI senza necessità di NGINX / Apache in prima validazione (Single-Container distribution deployment).

## Stato Storico
Decisione assunta come riferimento fondativo (Ottobre 2026).
