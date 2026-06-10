from pathlib import Path
import json


def md_cell(cell_id: str, text: str):
    return {
        "cell_type": "markdown",
        "metadata": {"id": cell_id, "language": "markdown"},
        "source": [line + "\n" for line in text.strip("\n").splitlines()],
    }


def code_cell(cell_id: str, text: str):
    return {
        "cell_type": "code",
        "execution_count": None,
        "metadata": {"id": cell_id, "language": "python"},
        "outputs": [],
        "source": [line + "\n" for line in text.strip("\n").splitlines()],
    }


cells = [
    md_cell(
        "intro",
        """# EDA transport - Paris et Ile-de-France

## Objectif
Construire une base d'analyse lisible pour de futurs indicateurs cartographiques sur les transports a Paris et en Ile-de-France.
Le notebook est volontairement ecrit avec du code simple, repetitif et local a chaque section pour rester facile a relire et a modifier.

## Jeux de donnees analyses
- Velib - stations et disponibilite temps reel
- Comptage multimodal - volumes par mode et par trajectoire
- Gares IDF - gares et dessertes par ligne
- Arrets IDF - referentiel des arrets

## Fil conducteur
1. Comprendre le sens metier de chaque colonne.
2. Tester la qualite de donnees et les duplications.
3. Identifier ce qui est reellement exploitable pour des indices spatiaux.
4. Conclure sur les jointures possibles et les limites de chaque source.""",
    ),
    md_cell(
        "setup-md",
        """## 1. Parametres generaux

Cette section garde uniquement les imports et les options d'affichage.
Les traitements de chargement, profilage et visualisation sont ensuite places directement dans les sections ou ils servent.""",
    ),
    code_cell(
        "setup-code",
        """import json
import time
import warnings
from datetime import datetime

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests
import seaborn as sns
from IPython.display import display

warnings.filterwarnings('ignore')
pd.set_option('display.max_columns', None)
pd.set_option('display.max_rows', 100)
pd.set_option('display.width', None)
pd.set_option('display.max_colwidth', 120)

sns.set_theme(style='whitegrid', context='notebook')
plt.rcParams['figure.figsize'] = (12, 5)

print('Configuration chargee')
print(f'Pandas: {pd.__version__} | Numpy: {np.__version__}')
print(f"Date d'analyse: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")""",
    ),
    code_cell(
        "api-loader",
        """def fetch_api_data(base_url, dataset_id, rows=2000, batch_size=100, max_retries=5, pause_seconds=2):
    records = []
    offset = 0
    headers = {
        'User-Agent': 'Mozilla/5.0',
        'Accept': 'application/json',
        'Connection': 'close',
    }

    while len(records) < rows:
        url = f"{base_url}/{dataset_id}/records"
        params = {
            'limit': min(batch_size, rows - len(records)),
            'offset': offset,
        }

        payload = None
        last_error = None

        for attempt in range(max_retries):
            try:
                response = requests.get(url, params=params, headers=headers, timeout=(10, 60))
                response.raise_for_status()
                payload = response.json()
                break
            except requests.exceptions.RequestException as error:
                last_error = error
                wait_time = pause_seconds * (attempt + 1)
                print(f"Tentative {attempt + 1}/{max_retries} echouee pour {dataset_id} a l'offset {offset}: {error}")
                if attempt < max_retries - 1:
                    print(f'Nouvel essai dans {wait_time} seconde(s)...')
                    time.sleep(wait_time)

        if payload is None:
            raise RuntimeError(
                f"Echec du chargement API pour {dataset_id} apres {max_retries} tentatives. "
                f"Derniere erreur: {last_error}"
            )

        batch = payload.get('results', [])
        if not batch:
            break

        records.extend(batch)
        offset += len(batch)

        total_count = payload.get('total_count')
        if total_count is not None and offset >= total_count:
            break

        time.sleep(0.3)

    return pd.DataFrame(records)

print('Fonction API prete')""",
    ),
    md_cell(
        "velib-md",
        """## 2. Analyse du dataset Velib

Points de lecture metier a verifier: taille de station via la capacite, presence de borne de paiement, communes couvertes, position par arrondissement, et separation entre variables statiques et variables dynamiques temps reel.
L'objectif est de savoir si cette source peut alimenter des indices de densite de stations, de capacite cumulee et d'accessibilite Velib a l'echelle d'un arrondissement.""",
    ),
    code_cell(
        "velib-load",
        """velib_cfg = {
    'base_url': 'https://opendata.paris.fr/api/explore/v2.1/catalog/datasets',
    'dataset_id': 'velib-disponibilite-en-temps-reel',
    'rows': 2000,
    'batch_size': 100,
}

df_velib = fetch_api_data(
    velib_cfg['base_url'],
    velib_cfg['dataset_id'],
    rows=velib_cfg['rows'],
    batch_size=velib_cfg['batch_size'],
)

print('=== Velib ===')
print(f'Lignes: {len(df_velib)} | Colonnes: {df_velib.shape[1]}')
print('Colonnes:')
print(df_velib.columns.tolist())
display(df_velib.head(5))

df_velib_hashable = df_velib.copy()
for column in df_velib_hashable.columns:
    df_velib_hashable[column] = df_velib_hashable[column].apply(
        lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (list, dict)) else value
    )
print(f'Doublons exacts: {int(df_velib_hashable.duplicated().sum())}')

velib_profile_rows = []
for column in df_velib.columns:
    series = df_velib[column]
    safe_series = df_velib_hashable[column]
    velib_profile_rows.append({
        'column': column,
        'dtype': str(series.dtype),
        'null_count': int(series.isna().sum()),
        'null_pct': round(float(series.isna().mean() * 100), 2),
        'n_unique': int(safe_series.nunique(dropna=True)),
        'sample_values': ' | '.join(safe_series.dropna().astype(str).unique()[:5]) if safe_series.notna().any() else '-',
    })

velib_profile = pd.DataFrame(velib_profile_rows).sort_values(['null_pct', 'n_unique'], ascending=[False, False]).reset_index(drop=True)
display(velib_profile)

velib_numeric_cols = df_velib.select_dtypes(include='number').columns.tolist()
if velib_numeric_cols:
    display(df_velib[velib_numeric_cols].describe().T)""",
    ),
    md_cell(
        "velib-dict",
        """### Dictionnaire de donnees

- `stationcode` : identifiant technique de la station
- `name` : nom complet de la station
- `nom_arrondissement_communes` : commune ou arrondissement rattache a la station
- `capacity` : capacite totale de station, utile comme proxy de taille
- `numbikesavailable` : nombre total de velos disponibles
- `numdocksavailable` : nombre total de bornettes libres
- `mechanical` : nombre de velos mecaniques disponibles
- `ebike` : nombre de velos electriques disponibles
- `is_renting` : station ouverte a la prise de velo
- `is_returning` : station ouverte au retour de velo
- `coordonnees_geo` ou champ equivalent : position de la station
- `creditcard` ou champ equivalent : presence d'un dispositif de paiement, si disponible dans la source""",
    ),
    code_cell(
        "velib-analysis-1",
        """print('Colonnes statiques probables:')
print([column for column in df_velib.columns if column not in ['numbikesavailable', 'numdocksavailable', 'mechanical', 'ebike', 'is_renting', 'is_returning', 'duedate']])

print('\\nColonnes dynamiques probables:')
print([column for column in ['numbikesavailable', 'numdocksavailable', 'mechanical', 'ebike', 'is_renting', 'is_returning', 'duedate'] if column in df_velib.columns])

if 'capacity' in df_velib.columns:
    print('\\nDistribution de la capacite des stations')
    print(df_velib['capacity'].describe())

    sns.histplot(df_velib['capacity'].dropna(), bins=30, kde=True, color='steelblue')
    plt.title('Distribution de la capacite des stations Velib')
    plt.xlabel('Capacite')
    plt.ylabel('Nombre de stations')
    plt.tight_layout()
    plt.show()

    capacity_counts = df_velib['capacity'].astype(str).value_counts(dropna=False).reset_index()
    capacity_counts.columns = ['capacity', 'count']
    capacity_counts = capacity_counts.sort_values('capacity', key=lambda x: pd.to_numeric(x, errors='coerce')).reset_index(drop=True)
    display(capacity_counts.head(15))

    plt.figure(figsize=(18, 6))
    sns.barplot(data=capacity_counts, x='capacity', y='count', color='steelblue')
    plt.title('Nombre de stations Velib par capacite')
    plt.xlabel('Capacite')
    plt.ylabel('Nombre de stations')
    plt.tight_layout()
    plt.show()""",
    ),
    code_cell(
        "velib-analysis-2",
        """velib_paris = df_velib.copy()

if 'nom_arrondissement_communes' in df_velib.columns:
    commune_counts = df_velib['nom_arrondissement_communes'].astype(str).value_counts(dropna=False).reset_index()
    commune_counts.columns = ['nom_arrondissement_communes', 'count']
    print('Stations par commune au total :', int(commune_counts['count'].sum()))
    display(commune_counts.head(15))

    sns.barplot(data=commune_counts.head(20), x='count', y='nom_arrondissement_communes', color='steelblue')
    plt.title('Nombre de stations Velib par commune')
    plt.xlabel('Nombre de stations')
    plt.ylabel('Commune')
    plt.tight_layout()
    plt.show()

    paris_mask = df_velib['nom_arrondissement_communes'].astype(str).str.contains('Paris', case=False, na=False)
    if paris_mask.any():
        velib_paris = df_velib.loc[paris_mask].copy()
        print(f'Filtrage Paris applique: {len(velib_paris)} lignes conservees')
    else:
        print('Aucun filtrage Paris explicite n a pu etre applique')

if 'nom_arrondissement_communes' in velib_paris.columns:
    paris_counts = velib_paris['nom_arrondissement_communes'].astype(str).value_counts(dropna=False).reset_index()
    paris_counts.columns = ['nom_arrondissement_communes', 'count']
    display(paris_counts.head(20))

    sns.barplot(data=paris_counts.head(20), x='count', y='nom_arrondissement_communes', color='coral')
    plt.title('Stations Velib dans Paris par arrondissement / libelle communal')
    plt.xlabel('Nombre de stations')
    plt.ylabel('Territoire')
    plt.tight_layout()
    plt.show()

if 'capacity' in velib_paris.columns and 'nom_arrondissement_communes' in velib_paris.columns:
    capacity_by_arr = velib_paris.groupby('nom_arrondissement_communes', dropna=False)['capacity'].agg(['count', 'sum', 'mean']).reset_index()
    capacity_by_arr.columns = ['nom_arrondissement_communes', 'nb_stations', 'capacity_cumulee', 'capacity_moyenne']
    capacity_by_arr = capacity_by_arr.sort_values('capacity_cumulee', ascending=False)
    display(capacity_by_arr.head(15))

if 'creditcard' in velib_paris.columns:
    payment_counts = velib_paris['creditcard'].astype(str).value_counts(dropna=False).reset_index()
    payment_counts.columns = ['creditcard', 'count']
    display(payment_counts)

    sns.barplot(data=payment_counts, x='count', y='creditcard', color='seagreen')
    plt.title('Presence d un dispositif de paiement Velib')
    plt.xlabel('Nombre de stations')
    plt.ylabel('creditcard')
    plt.tight_layout()
    plt.show()
else:
    print('La colonne creditcard n est pas presente dans cet echantillon Velib')""",
    ),
    code_cell(
        "velib-analysis-3",
        """dynamic_cols = [column for column in ['numbikesavailable', 'numdocksavailable', 'mechanical', 'ebike'] if column in velib_paris.columns]
if dynamic_cols:
    print('Statistiques descriptives sur les variables dynamiques')
    display(velib_paris[dynamic_cols].describe())

    fig, axes = plt.subplots(1, len(dynamic_cols), figsize=(5 * len(dynamic_cols), 4))
    if len(dynamic_cols) == 1:
        axes = [axes]
    for ax, column in zip(axes, dynamic_cols):
        sns.histplot(velib_paris[column].dropna(), bins=30, kde=True, color='steelblue', ax=ax)
        ax.set_title(column)
        ax.set_xlabel(column)
    plt.tight_layout()
    plt.show()

if 'is_renting' in velib_paris.columns:
    renting_counts = velib_paris['is_renting'].astype(str).value_counts(dropna=False).reset_index()
    renting_counts.columns = ['is_renting', 'count']
    display(renting_counts)

if 'is_returning' in velib_paris.columns:
    returning_counts = velib_paris['is_returning'].astype(str).value_counts(dropna=False).reset_index()
    returning_counts.columns = ['is_returning', 'count']
    display(returning_counts)

print('Conclusion Velib:')
print('- La capacite est exploitable comme taille de station.')
print('- Les colonnes de stock en temps reel doivent etre traitees comme des mesures instantanees.')
print('- Le filtrage Paris est indispensable avant de calculer des indicateurs par arrondissement.')
print('- Les indicateurs les plus naturels sont la densite de stations, la capacite cumulee et la part de stations avec paiement si la colonne existe.')""",
    ),
    md_cell(
        "comptage-md",
        """## 3. Analyse du dataset Comptage multimodal

Cette source doit etre lue avec prudence: il faut verifier si elle porte seulement un point de comptage, une trajectoire locale ou une geometrie plus riche de voie.
L'analyse vise surtout a identifier les categories de vehicules, les differences par type de file, par sens et par trajectoire, puis a evaluer ce qui peut vraiment etre agrege spatialement.""",
    ),
    code_cell(
        "comptage-load",
        """comptage_cfg = {
    'base_url': 'https://parisdata.opendatasoft.com/api/explore/v2.1/catalog/datasets',
    'dataset_id': 'comptage-multimodal-comptages',
    'rows': 2000,
    'batch_size': 100,
}

df_comptage = fetch_api_data(
    comptage_cfg['base_url'],
    comptage_cfg['dataset_id'],
    rows=comptage_cfg['rows'],
    batch_size=comptage_cfg['batch_size'],
)

print('=== Comptage multimodal ===')
print(f'Lignes: {len(df_comptage)} | Colonnes: {df_comptage.shape[1]}')
print('Colonnes:')
print(df_comptage.columns.tolist())
display(df_comptage.head(5))

df_comptage_hashable = df_comptage.copy()
for column in df_comptage_hashable.columns:
    df_comptage_hashable[column] = df_comptage_hashable[column].apply(
        lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (list, dict)) else value
    )
print(f'Doublons exacts: {int(df_comptage_hashable.duplicated().sum())}')

comptage_profile_rows = []
for column in df_comptage.columns:
    series = df_comptage[column]
    safe_series = df_comptage_hashable[column]
    comptage_profile_rows.append({
        'column': column,
        'dtype': str(series.dtype),
        'null_count': int(series.isna().sum()),
        'null_pct': round(float(series.isna().mean() * 100), 2),
        'n_unique': int(safe_series.nunique(dropna=True)),
        'sample_values': ' | '.join(safe_series.dropna().astype(str).unique()[:5]) if safe_series.notna().any() else '-',
    })

comptage_profile = pd.DataFrame(comptage_profile_rows).sort_values(['null_pct', 'n_unique'], ascending=[False, False]).reset_index(drop=True)
display(comptage_profile)

comptage_numeric_cols = df_comptage.select_dtypes(include='number').columns.tolist()
if comptage_numeric_cols:
    display(df_comptage[comptage_numeric_cols].describe().T)""",
    ),
    md_cell(
        "comptage-dict",
        """### Dictionnaire de donnees

- `id_site` : identifiant unique du point ou site de comptage
- `label` : libelle descriptif du site
- `coordonnees_geo` : position geographique du capteur ou du site
- `mode` : categorie ou mode compte
- `nb_usagers` : volume compte sur la periode
- `voie` : type de file ou de voie observee
- `sens` : sens de circulation
- `trajectoire` ou champ equivalent : orientation locale du flux
- `date` / `heure` / `t1` / `t2` selon la structure : temporalite de la mesure

Point de vigilance: ce jeu sert surtout a decrire un flux local mesure par capteur, pas toute la largeur de la rue ni tout l axe urbain.""",
    ),
    code_cell(
        "comptage-analysis-1",
        """if 'coordonnees_geo' in df_comptage.columns:
    print('La source contient une geometrie ponctuelle exploitable via coordonnees_geo')
    display(df_comptage['coordonnees_geo'].head(10))
else:
    print('Aucune colonne coordonnees_geo explicite dans cet echantillon')

if 'nb_usagers' in df_comptage.columns:
    print(df_comptage['nb_usagers'].describe())

    sns.histplot(df_comptage['nb_usagers'].dropna(), bins=40, kde=True, color='steelblue')
    plt.title('Distribution des volumes de comptage (nb_usagers)')
    plt.xlabel("Nombre d'usagers par heure")
    plt.ylabel('Nombre de mesures')
    plt.tight_layout()
    plt.show()

if 'mode' in df_comptage.columns:
    mode_counts = df_comptage['mode'].astype(str).value_counts(dropna=False).reset_index()
    mode_counts.columns = ['mode', 'count']
    display(mode_counts)

    sns.barplot(data=mode_counts, x='count', y='mode', color='steelblue')
    plt.title('Repartition par mode de transport')
    plt.xlabel('Nombre de mesures')
    plt.ylabel('Mode')
    plt.tight_layout()
    plt.show()""",
    ),
    code_cell(
        "comptage-analysis-2",
        """if 'voie' in df_comptage.columns:
    voie_counts = df_comptage['voie'].astype(str).value_counts(dropna=False).reset_index()
    voie_counts.columns = ['voie', 'count']
    display(voie_counts)

    sns.barplot(data=voie_counts, x='count', y='voie', color='coral')
    plt.title('Repartition par type de voie')
    plt.xlabel('Nombre de mesures')
    plt.ylabel('Type de voie')
    plt.tight_layout()
    plt.show()

if 'sens' in df_comptage.columns:
    sens_counts = df_comptage['sens'].astype(str).value_counts(dropna=False).reset_index()
    sens_counts.columns = ['sens', 'count']
    display(sens_counts)

    sns.barplot(data=sens_counts, x='count', y='sens', color='seagreen')
    plt.title('Repartition par sens de circulation')
    plt.xlabel('Nombre de mesures')
    plt.ylabel('Sens')
    plt.tight_layout()
    plt.show()

if 'trajectoire' in df_comptage.columns:
    trajectoire_counts = df_comptage['trajectoire'].astype(str).value_counts(dropna=False).reset_index()
    trajectoire_counts.columns = ['trajectoire', 'count']
    display(trajectoire_counts.head(20))

if 'mode' in df_comptage.columns and 'nb_usagers' in df_comptage.columns:
    avg_by_mode = df_comptage.groupby('mode', dropna=False)['nb_usagers'].mean().sort_values(ascending=False).reset_index()
    avg_by_mode.columns = ['mode', 'nb_usagers_moyen']
    display(avg_by_mode)

    sns.barplot(data=avg_by_mode, x='nb_usagers_moyen', y='mode', color='steelblue')
    plt.title('Volume moyen par mode de transport')
    plt.xlabel('Nb usagers moyen par heure')
    plt.ylabel('Mode')
    plt.tight_layout()
    plt.show()""",
    ),
    code_cell(
        "comptage-analysis-3",
        """if 'nom_arrondissement' in df_comptage.columns:
    arr_counts = df_comptage['nom_arrondissement'].astype(str).value_counts(dropna=False).reset_index()
    arr_counts.columns = ['nom_arrondissement', 'count']
    display(arr_counts.head(20))
elif 'arrondissement' in df_comptage.columns:
    arr_counts = df_comptage['arrondissement'].astype(str).value_counts(dropna=False).reset_index()
    arr_counts.columns = ['arrondissement', 'count']
    display(arr_counts.head(20))
else:
    print('Aucun arrondissement explicite dans cet echantillon: une jointure spatiale externe sera sans doute necessaire')

print('Conclusion Comptage multimodal:')
print('- La source semble decrire un point ou site de comptage localise, pas une rue complete.')
print('- Elle est utile pour reperer les axes les plus frequentes seulement si le site, la trajectoire et la voie sont bien interpretes.')
print('- Elle ne permet pas a elle seule de mesurer toute la largeur de route ni l ensemble d un boulevard.')
print('- Les indicateurs les plus realistes sont des flux locaux, des mix modaux et des comparaisons par sens ou par voie.')""",
    ),
    md_cell(
        "gares-md",
        """## 4. Analyse du dataset Gares IDF

Ici, il faut distinguer la gare physique de la desserte par ligne. Un meme lieu peut apparaitre plusieurs fois s'il est desservi par plusieurs lignes, ce qui cree des doublons logiques mais utiles pour mesurer l'intensite de desserte.
L'enjeu est de compter les modes, les gares uniques et les relations gare-ligne qui peuvent alimenter un indice d'accessibilite ferrie ou de multimodalite.""",
    ),
    code_cell(
        "gares-load",
        """gares_cfg = {
    'base_url': 'https://data.iledefrance-mobilites.fr/api/explore/v2.1/catalog/datasets',
    'dataset_id': 'emplacement-des-gares-idf',
    'rows': 500,
    'batch_size': 100,
}

df_gares = fetch_api_data(
    gares_cfg['base_url'],
    gares_cfg['dataset_id'],
    rows=gares_cfg['rows'],
    batch_size=gares_cfg['batch_size'],
)

print('=== Gares IDF ===')
print(f'Lignes: {len(df_gares)} | Colonnes: {df_gares.shape[1]}')
print('Colonnes:')
print(df_gares.columns.tolist())
display(df_gares.head(5))

df_gares_hashable = df_gares.copy()
for column in df_gares_hashable.columns:
    df_gares_hashable[column] = df_gares_hashable[column].apply(
        lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (list, dict)) else value
    )
print(f'Doublons exacts: {int(df_gares_hashable.duplicated().sum())}')

gares_profile_rows = []
for column in df_gares.columns:
    series = df_gares[column]
    safe_series = df_gares_hashable[column]
    gares_profile_rows.append({
        'column': column,
        'dtype': str(series.dtype),
        'null_count': int(series.isna().sum()),
        'null_pct': round(float(series.isna().mean() * 100), 2),
        'n_unique': int(safe_series.nunique(dropna=True)),
        'sample_values': ' | '.join(safe_series.dropna().astype(str).unique()[:5]) if safe_series.notna().any() else '-',
    })

gares_profile = pd.DataFrame(gares_profile_rows).sort_values(['null_pct', 'n_unique'], ascending=[False, False]).reset_index(drop=True)
display(gares_profile)

gares_numeric_cols = df_gares.select_dtypes(include='number').columns.tolist()
if gares_numeric_cols:
    display(df_gares[gares_numeric_cols].describe().T)""",
    ),
    md_cell(
        "gares-dict",
        """### Dictionnaire de donnees

- `id_gares` : identifiant de la gare physique
- `nom_gares` : nom de la gare
- `mode` : mode de transport desservi
- `res_com` : ligne ou reseau commercial rattache
- `indice_lig` : identifiant ou indice de ligne
- `exploitant` : operateur
- `geo_point_2d` : point geographique de la gare
- `x`, `y` : coordonnees projetees ou complementaires selon la source

Point de vigilance: une meme gare peut apparaitre plusieurs fois parce qu elle est decrite ligne par ligne.""",
    ),
    code_cell(
        "gares-analysis-1",
        """if 'mode' in df_gares.columns:
    mode_counts = df_gares['mode'].astype(str).value_counts(dropna=False).reset_index()
    mode_counts.columns = ['mode', 'count']
    display(mode_counts)

    sns.barplot(data=mode_counts, x='count', y='mode', color='steelblue')
    plt.title('Repartition des gares par mode de transport')
    plt.xlabel('Nombre de lignes de desserte observees')
    plt.ylabel('Mode')
    plt.tight_layout()
    plt.show()

if 'exploitant' in df_gares.columns:
    exploitant_counts = df_gares['exploitant'].astype(str).value_counts(dropna=False).reset_index()
    exploitant_counts.columns = ['exploitant', 'count']
    display(exploitant_counts)

    sns.barplot(data=exploitant_counts, x='count', y='exploitant', color='coral')
    plt.title('Repartition par exploitant')
    plt.xlabel('Nombre de lignes de desserte observees')
    plt.ylabel('Exploitant')
    plt.tight_layout()
    plt.show()

if 'res_com' in df_gares.columns:
    ligne_counts = df_gares['res_com'].astype(str).value_counts(dropna=False).head(15).reset_index()
    ligne_counts.columns = ['res_com', 'count']
    display(ligne_counts)

    sns.barplot(data=ligne_counts, x='count', y='res_com', color='seagreen')
    plt.title('Top 15 des lignes les plus representees')
    plt.xlabel('Nombre de gares desservies')
    plt.ylabel('Ligne')
    plt.tight_layout()
    plt.show()""",
    ),
    code_cell(
        "gares-analysis-2",
        """if 'id_gares' in df_gares.columns:
    print('Nombre de gares physiques uniques :', df_gares['id_gares'].nunique(dropna=True))

if 'nom_gares' in df_gares.columns and 'res_com' in df_gares.columns:
    service_counts = df_gares.groupby('nom_gares', dropna=False)['res_com'].nunique().sort_values(ascending=False).reset_index()
    service_counts.columns = ['nom_gares', 'nb_lignes']
    display(service_counts.head(15))

    sns.barplot(data=service_counts.head(15), x='nb_lignes', y='nom_gares', color='steelblue')
    plt.title('Gares desservies par le plus de lignes')
    plt.xlabel('Nombre de lignes distinctes')
    plt.ylabel('Gare')
    plt.tight_layout()
    plt.show()

if 'id_gares' in df_gares.columns and 'res_com' in df_gares.columns:
    print('Doublons exacts sur le couple gare-ligne :', int(df_gares.duplicated(subset=['id_gares', 'res_com']).sum()))

print('Conclusion Gares IDF:')
print('- Il faut distinguer la gare physique et la desserte par ligne.')
print('- La source est tres utile pour mesurer la multimodalite et l intensite de desserte.')
print('- Pour un indicateur spatial, il faudra souvent dedoublonner au niveau gare puis compter les lignes, modes ou exploitants.')""",
    ),
    md_cell(
        "arrets-md",
        """## 5. Analyse du dataset Arrets IDF

Un arret de reference est le lieu ou le voyageur attend, monte ou descend des vehicules; c'est souvent plus fin et plus general que la gare au sens ferroviaire.
Cette source est importante pour les indicateurs de maillage fin et de proximite, notamment quand on veut aller au plus pres de l'offre de transport et pas seulement des grandes gares.""",
    ),
    code_cell(
        "arrets-load",
        """arrets_cfg = {
    'base_url': 'https://data.iledefrance-mobilites.fr/api/explore/v2.1/catalog/datasets',
    'dataset_id': 'arrets',
    'rows': 2000,
    'batch_size': 100,
}

df_arrets = fetch_api_data(
    arrets_cfg['base_url'],
    arrets_cfg['dataset_id'],
    rows=arrets_cfg['rows'],
    batch_size=arrets_cfg['batch_size'],
)

print('=== Arrets IDF ===')
print(f'Lignes: {len(df_arrets)} | Colonnes: {df_arrets.shape[1]}')
print('Colonnes:')
print(df_arrets.columns.tolist())
display(df_arrets.head(5))

df_arrets_hashable = df_arrets.copy()
for column in df_arrets_hashable.columns:
    df_arrets_hashable[column] = df_arrets_hashable[column].apply(
        lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (list, dict)) else value
    )
print(f'Doublons exacts: {int(df_arrets_hashable.duplicated().sum())}')

arrets_profile_rows = []
for column in df_arrets.columns:
    series = df_arrets[column]
    safe_series = df_arrets_hashable[column]
    arrets_profile_rows.append({
        'column': column,
        'dtype': str(series.dtype),
        'null_count': int(series.isna().sum()),
        'null_pct': round(float(series.isna().mean() * 100), 2),
        'n_unique': int(safe_series.nunique(dropna=True)),
        'sample_values': ' | '.join(safe_series.dropna().astype(str).unique()[:5]) if safe_series.notna().any() else '-',
    })

arrets_profile = pd.DataFrame(arrets_profile_rows).sort_values(['null_pct', 'n_unique'], ascending=[False, False]).reset_index(drop=True)
display(arrets_profile)

arrets_numeric_cols = df_arrets.select_dtypes(include='number').columns.tolist()
if arrets_numeric_cols:
    display(df_arrets[arrets_numeric_cols].describe().T)""",
    ),
    md_cell(
        "arrets-dict",
        """### Dictionnaire de donnees

- `arrid` : identifiant d arret de reference
- `arrname` : nom d arret
- `arrtype` : type d arret ou mode principal
- `arrfarezone` : zone tarifaire
- `arraccessibility` : information d accessibilite
- `arrtown` : commune rattachee
- `arrgeopoint` ou champ equivalent : position geographique

Point de vigilance: un arret de reference peut etre plus pertinent qu une gare pour la proximite locale, mais moins adapte a la mesure de desserte lourde ferroviaire.""",
    ),
    code_cell(
        "arrets-analysis-1",
        """if 'arrtype' in df_arrets.columns:
    type_counts = df_arrets['arrtype'].astype(str).value_counts(dropna=False).reset_index()
    type_counts.columns = ['arrtype', 'count']
    display(type_counts)

    sns.barplot(data=type_counts, x='count', y='arrtype', color='steelblue')
    plt.title("Repartition par type d'arret")
    plt.xlabel("Nombre d'arrets")
    plt.ylabel('Type')
    plt.tight_layout()
    plt.show()

if 'arrfarezone' in df_arrets.columns:
    zone_counts = df_arrets['arrfarezone'].astype(str).value_counts(dropna=False).reset_index()
    zone_counts.columns = ['arrfarezone', 'count']
    display(zone_counts)

    sns.barplot(data=zone_counts, x='count', y='arrfarezone', color='coral')
    plt.title('Repartition par zone tarifaire')
    plt.xlabel("Nombre d'arrets")
    plt.ylabel('Zone tarifaire')
    plt.tight_layout()
    plt.show()

if 'arraccessibility' in df_arrets.columns:
    access_counts = df_arrets['arraccessibility'].astype(str).value_counts(dropna=False).reset_index()
    access_counts.columns = ['arraccessibility', 'count']
    display(access_counts)

    sns.barplot(data=access_counts, x='count', y='arraccessibility', color='seagreen')
    plt.title("Accessibilite des arrets")
    plt.xlabel("Nombre d'arrets")
    plt.ylabel('Accessibilite')
    plt.tight_layout()
    plt.show()""",
    ),
    code_cell(
        "arrets-analysis-2",
        """if 'arrtown' in df_arrets.columns:
    town_counts = df_arrets['arrtown'].astype(str).value_counts(dropna=False).head(20).reset_index()
    town_counts.columns = ['arrtown', 'count']
    display(town_counts)

    sns.barplot(data=town_counts, x='count', y='arrtown', color='steelblue')
    plt.title("Top 20 communes avec le plus d'arrets")
    plt.xlabel("Nombre d'arrets")
    plt.ylabel('Commune')
    plt.tight_layout()
    plt.show()

if 'arrname' in df_arrets.columns and 'nom_gares' in df_gares.columns:
    common = set(df_arrets['arrname'].dropna().astype(str).str.strip().str.lower().unique()) & set(df_gares['nom_gares'].dropna().astype(str).str.strip().str.lower().unique())
    print(f'Chevauchement nominal arrets / gares : {len(common)} noms en commun (indicatif)')
    print('Une vraie jointure doit privilegier les coordonnees ou des identifiants normalises')

print('Conclusion Arrets IDF:')
print('- Ce jeu est tres utile pour le maillage fin et la proximite locale.')
print('- Il peut etre plus pertinent que les gares pour mesurer la couverture de transport du quotidien.')
print('- Les jointures texte seules restent fragiles; une approche spatiale sera souvent preferable.')""",
    ),
    md_cell(
        "synthese-md",
        """## 6. Synthese des variables utiles et recommandations

Cette section rassemble les variables a conserver, celles a traiter avec prudence et les pistes d'indicateurs cartographiques possibles.
L'objectif n'est pas de figer des indices tout de suite, mais de preparer une couche de travail stable pour la phase suivante.""",
    ),
    code_cell(
        "synthese-code",
        """summary_rows = [
    {
        'Dataset': 'Velib',
        'Granularite': 'station + etat temps reel',
        'Geometrie': 'point station / coordonnees si presentes',
        'Variables cles': 'capacite, disponibilite, bornes, commune, arrondissement, paiement',
        'Limites': 'temps reel, couverture parfois au-dela de Paris',
        'Idees d indicateurs': 'densite de stations, capacite cumulee, accessibilite Velib, part de stations avec paiement',
    },
    {
        'Dataset': 'Comptage multimodal',
        'Granularite': 'site / point de comptage horaire',
        'Geometrie': 'point localise ou trajectoire locale',
        'Variables cles': 'mode, volume, type de voie, sens, trajectoire',
        'Limites': 'lecture partielle de l axe si seule une localisation ponctuelle existe',
        'Idees d indicateurs': 'intensite de trafic, mix modal, pression par mode, comparaison sens / trajectoires',
    },
    {
        'Dataset': 'Gares IDF',
        'Granularite': 'gare physique + desserte par ligne',
        'Geometrie': 'point gare / station',
        'Variables cles': 'gare, ligne, mode, exploitant',
        'Limites': 'doublons logiques par ligne',
        'Idees d indicateurs': 'accessibilite ferree, intensite de desserte, multimodalite',
    },
    {
        'Dataset': 'Arrets IDF',
        'Granularite': 'arret de reference fin',
        'Geometrie': 'point d arret',
        'Variables cles': 'identifiant, libelle, type, zone, commune',
        'Limites': 'jointure textuelle seule insuffisante',
        'Idees d indicateurs': 'maillage fin, proximite transport, couverture locale, comparaison avec les gares',
    },
]

summary_df = pd.DataFrame(summary_rows)
display(summary_df)

print('Recommandations de travail:')
print('1. Conserver les identifiants, noms, categories, geometries et variables de desserte.')
print('2. Isoler les colonnes purement dynamiques avant de calculer des indices structurels.')
print('3. Prioriser les jointures spatiales ou par identifiant avant les jointures texte.')
print('4. Utiliser Velib pour Paris, Gares pour la desserte ferree et Arrets pour le maillage fin.')
print('5. Garder le comptage multimodal comme source de pression de trafic local, pas comme couche complete d occupation territoriale.')""",
    ),
]

notebook = {
    "cells": cells,
    "metadata": {
        "kernelspec": {
            "display_name": "Python 3",
            "language": "python",
            "name": "python3",
        },
        "language_info": {
            "name": "python",
            "version": "3.13.1",
        },
    },
    "nbformat": 4,
    "nbformat_minor": 5,
}

Path("eda_transport.ipynb").write_text(json.dumps(notebook, ensure_ascii=False, indent=2), encoding="utf-8")
print("Notebook written to eda_transport.ipynb")
