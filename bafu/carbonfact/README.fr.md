# Les procédés Carbonfact, avec BAFU en arrière-plan

Carbonfact publie huit bases au format Brightway (un classeur `.xlsx` chacune),
une par étape de fabrication textile : filature, tricotage, tissage, teinture,
impression, ennoblissement, confection, usage. Chacune nomme ses fournisseurs
dans ecoinvent 3.12 cut-off, et aucune n’appelle les procédés d’une autre : les
procédés communs, comme le mélange de chaleur, sont recopiés dans chaque base qui
s’en sert. `carbonfact_bafu.py` travaille donc base par base, et une nouvelle
base se traite de la même façon que les huit premières ; seul `merge` en prend
plusieurs. Il a quatre commandes, indépendantes :

| Commande | Ce qu’elle fait | Ce qu’elle écrit |
|---|---|---|
| `relink` | la BAFUisation : chaque fournisseur ecoinvent remplacé par son équivalent BAFU | la base sur BAFU, et la correspondance appliquée |
| `compare` | calcule deux versions d’une base, chacune sur son arrière-plan, et les compare | un tableau, un graphique, un résumé dans le terminal |
| `quality` | le rapport de qualité structurel du moteur sur une base | un tableau des défauts trouvés |
| `merge` | réunit plusieurs bases en une, chaque activité commune une seule fois | la base réunie |

Le plus souvent, on enchaîne les trois sur une base : `relink`, puis `compare`
entre la base publiée et sa version BAFU, puis `quality` sur la version BAFU. `merge` vient à la fin, quand on veut une
seule base pour toutes les étapes.

[README.md](README.md) dit la même chose en anglais.

## Installer, une fois (Windows)

Le script ne demande rien d’autre que `uv`, qui apporte son propre Python et les
bibliothèques que le script déclare ; le script télécharge lui-même le moteur
VoLCA.

Ouvrir PowerShell (menu Démarrer, taper `PowerShell`) et installer `uv` :

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Fermer PowerShell et le rouvrir, pour qu’il trouve `uv`. Sous Linux ou macOS,
`curl -LsSf https://astral.sh/uv/install.sh | sh` l’installe.

## Lancer

Chaque commande se lance dans PowerShell, sur une seule ligne, chaque chemin
entre guillemets, aucun ne finissant par `\` (PowerShell y lirait
l’échappement du guillemet fermant). Les exemples prennent la base de teinture.

`uv` va chercher le script dans ce dépôt : rien à télécharger à la main, et
chaque lancement prend la dernière version. Pour garder une version, remplacer
`main` dans l’adresse par un numéro de commit. Depuis une copie du dépôt, lancer
plutôt `uv run carbonfact_bafu.py` dans ce dossier.

Le premier lancement télécharge le moteur, et le moteur lit ecoinvent dans son
archive, ce qui prend plusieurs minutes. Il décompresse chaque archive dans un
dossier à côté d’elle, du même nom suivi de `.d`, et y garde ce qu’il a lu : les
lancements suivants prennent quelques secondes.

Chaque commande écrit à côté de la base qu’elle reçoit, sous le nom de cette
base suivi de ce qu’elle produit ; `--out DOSSIER` écrit ailleurs.
`uv run carbonfact_bafu.py <commande> --help` décrit chaque option.

### `relink` : la BAFUisation

```powershell
uv run https://raw.githubusercontent.com/MTES-MCT/ecobalyse-method-tooling/main/bafu/carbonfact/carbonfact_bafu.py relink "C:\data\Dyeing_Inventory_Brightway.xlsx" --mapping "C:\data\mapping.xlsx" --ecoinvent "C:\data\ecoinvent 3.12_cutoff_ecoSpold02.7z" --bafu "C:\data\BAFU-2026 v1_ecoSpold v1.zip"
```

Il faut :

- **la base**, un classeur Brightway dont les fournisseurs sont dans ecoinvent
  3.12 cut-off ;
- **la correspondance** d’ecoinvent 3.11 vers BAFU (`--mapping`), au choix :
  - le classeur de l’expert (`.xlsx`), feuille `Matching Full`, ses colonnes
    trouvées par leur titre, dans cet ordre : `EI 3.11 dataset`, `BAFU Name`,
    `Geography`, `unit`, `conversion`, quelles que soient les colonnes entre elles ;
  - ou la correspondance exportée depuis la page de correspondance de VoLCA
    (`.csv`), faite avec ecoinvent 3.11 comme source. Seules les lignes
    décidées sont lues : une suggestion que personne n’a acceptée ne relie
    rien ;
- **ecoinvent 3.12 cut-off** (`--ecoinvent`), son archive ecoSpold02. ecoinvent
  la livre en `.7z`, qui demande d’avoir installé
  [7-Zip](https://www.7-zip.org/) ; un `.zip` des mêmes fichiers ne demande
  rien ;
- **BAFU** (`--bafu`), son archive ecoSpold v1, le `.zip` tel que BAFU le
  distribue ;
- seulement si le script le demande, **la table de correspondance
  d’ecoinvent**, `Correspondence File v3.11 - v.3.12.xlsx` (`--correspondence`).
  La correspondance vers BAFU est écrite pour la 3.11 : un fournisseur renommé
  en 3.12 n’y est pas trouvé sous son nouveau nom. Le script porte les
  renommages dont les huit bases ont besoin (plus bas) et cherche tout autre
  fournisseur sous son propre nom ; s’il en manque un, il s’arrête et propose ce
  fichier, qui remplace alors les renommages inscrits dans le script.

Il écrit :

- `Dyeing_Inventory_Brightway.bafu.brightway.xlsx` : la base sur BAFU, chaque
  entrée écrite sous le procédé BAFU auquel elle est reliée, rattachée à une
  base nommée d’après l’archive BAFU (`bafu-2026-v1-ecospold-v1` pour
  `BAFU-2026 v1_ecoSpold v1.zip`). Relue à côté de BAFU sous ce nom, elle se
  relie aux mêmes fournisseurs sans correspondance, et donne les mêmes scores.
  `--format simapro`, `ecospold2`, `openlca` ou `ilcd` l’écrit dans un autre
  format, et le nom suit : `….bafu.simapro.csv`, `….bafu.ecospold2.zip`,
  `….bafu.openlca.zip`, `….bafu.ilcd.zip`. Le paquet ILCD désigne chaque
  fournisseur BAFU par son produit et son lieu, ce qui permet à un lecteur de
  le retrouver à côté de BAFU ;
- `Dyeing_Inventory_Brightway.bafu.mapping.csv` : pour chaque fournisseur
  ecoinvent, le procédé BAFU auquel il est relié, `opposite` dans `direction`
  quand BAFU le compte dans l’autre sens (plus bas), le jeu de données 3.11 sous
  lequel la correspondance a été lue, et un `status` qui dit pourquoi un
  fournisseur n’en a pas.

`--complement FICHIER` donne vos propres choix BAFU, dans les colonnes de ce
fichier, à la place de ceux inscrits dans le script (plus bas) ; comme eux, ils
ne remplissent que ce que la correspondance laisse sans procédé BAFU.

### `compare` : comparer deux versions d’une base

```powershell
uv run https://raw.githubusercontent.com/MTES-MCT/ecobalyse-method-tooling/main/bafu/carbonfact/carbonfact_bafu.py compare "C:\data\Dyeing_Inventory_Brightway.xlsx" "C:\data\Dyeing_Inventory_Brightway.bafu.brightway.xlsx" --ecoinvent "C:\data\ecoinvent 3.12_cutoff_ecoSpold02.7z" --bafu "C:\data\BAFU-2026 v1_ecoSpold v1.zip" --method "C:\data\EF-v3.1.zip" --reported "C:\data\Carbonfact_Dyeing_DB_LCIA.csv"
```

Les deux versions peuvent être dans n’importe quel format que lit le moteur, et
pas forcément le même : les activités s’apparient par leur nom, leur produit et
leur lieu. Le moteur cherche les fournisseurs de chaque version dans ecoinvent
et BAFU, et dit lesquels il a trouvés pour chacune. Il faut en plus **la méthode
d’impact** (`--method`), n’importe laquelle que lit le moteur : EF 3.1 en paquet
ILCD du JRC (`EF-v3.1.zip`) ou en version adapted exportée de SimaPro en CSV,
la saturation écologique 2021 en CSV SimaPro, etc. `--reported`, facultatif,
donne les résultats que Carbonfact publie pour la base
(`Carbonfact_<étape>_DB_LCIA.csv`), pour vérifier que la première version les
retrouve : il les lit sur la catégorie `Climate change` de la méthode, et
demande donc EF 3.1.

Il écrit, sous le nom de la seconde version :

- `….comparison.csv` : chaque activité, puis chaque catégorie, pour la
  première version et pour la seconde ;
- `….comparison.svg` : un nuage par catégorie, la première version en
  abscisse, la seconde en ordonnée ; les bandes grises marquent ±10 % et un
  facteur 2.

Le terminal dit, si `--reported` est donné, à quel point la première version
approche les chiffres de Carbonfact, puis, par catégorie, le rapport médian
entre les deux versions, la part des activités à moins de 10 %, d’un facteur
1,5, 2 et 10, et combien aucun rapport ne peut juger (un zéro ou un négatif
d’un côté), et finit par les plus grands écarts toutes catégories
confondues (`--top`, 20 par défaut) : un rapport n’a pas d’unité, donc un écart
sur une catégorie se classe face à un écart sur une autre. Une activité
présente d’un seul côté est listée, puis laissée de côté.

### `quality` : le rapport de qualité

```powershell
uv run https://raw.githubusercontent.com/MTES-MCT/ecobalyse-method-tooling/main/bafu/carbonfact/carbonfact_bafu.py quality "C:\data\Dyeing_Inventory_Brightway.bafu.brightway.xlsx"
```

Il ne demande que la base, dans n’importe quel format que lit le moteur, et
écrit `….quality.csv` : les défauts de structure que le moteur trouve, comme un
procédé sans référence, une allocation qui ne fait pas 100 % ou des métadonnées
manquantes. Les entrées que la base ne fabrique pas elle-même y sont signalées
pour information : elles viennent de l’arrière-plan. Ce rapport n’est pas un
indicateur de qualité des données (DQR) : le script ne lit ni n’écrit la DQR que
Carbonfact publie.

### `merge` : réunir plusieurs bases

```powershell
uv run https://raw.githubusercontent.com/MTES-MCT/ecobalyse-method-tooling/main/bafu/carbonfact/carbonfact_bafu.py merge "C:\data\Dyeing_Inventory_Brightway.bafu.brightway.xlsx" "C:\data\Printing_Inventory_Brightway.bafu.ecospold2.zip" `
  --bafu "C:\data\BAFU-2026 v1_ecoSpold v1.zip" --format ecospold2
```

Il prend des bases dans n’importe quels formats que lit le moteur, sur
ecoinvent, sur BAFU ou les unes sur l’un et les autres sur l’autre, et les
arrière-plans où elles prennent leurs fournisseurs : `--ecoinvent`, `--bafu`
ou les deux. Il écrit `merged.<format>.<extension>` dans le dossier courant
(`--name` et `--out` changent le nom et le dossier). Une activité que plusieurs
bases recopient, comme le mélange de chaleur, n’est gardée qu’une fois ; écrite
différemment dans deux d’entre elles, elle arrête la fusion, car choisir laquelle
garder ne revient pas au script.

`VOLCA_BINARY` et `VOLCA_DATA_DIR`, posées en variables d’environnement, font
tourner un moteur compilé localement au lieu de celui téléchargé.

## Ce qui a été mesuré

Avec la correspondance de l’expert du 28 octobre 2026 telle quelle, les choix
inscrits dans le script (plus bas) et EF 3.1 adapted 1.03, sur les huit bases :
sur ecoinvent, le changement climatique est à moins de 0,03 % du chiffre que
publie Carbonfact, pour chacune des 294 activités qui en ont un. Sur BAFU, le
rapport médian BAFU / ecoinvent vaut 0,84 sur le changement climatique, avec
99 % des activités à moins d’un facteur 1,5. Avec le paquet EF 3.1 du JRC, sur
la seule base de teinture, le changement climatique est à moins de 0,06 % du
chiffre de Carbonfact pour ses 38 activités, et le rapport médian vaut 0,92.

Le plus grand écart porte sur l’écotoxicité de l’eau douce, inorganiques,
environ cent fois plus basse sur BAFU pour « Fabric finishing,
Softening, Exhaust, Jet ». Il vient du marché des eaux usées d’ecoinvent pour
le reste du monde, qui envoie une partie de l’eau à la rivière sans traitement,
avec le chlore qu’elle contient, un flux qu’EF 3.1 pèse lourdement. BAFU traite
la même eau dans une station suisse. L’écart est une différence entre les deux
bases, pas une erreur de la reliaison.

La consommation d’eau sort légèrement négative sur BAFU pour le lavage et le
lavage à la main de la phase d’usage. C’était déjà le cas avant que le sens
soit lu, et ce n’est pas examiné ici.

## Quand il s’arrête

- **`none for … : not in the mapping`**, suivi de **`Name a BAFU supplier for
  each in a --complement file`** : certains fournisseurs ecoinvent n’ont pas
  d’équivalent BAFU. Copier `….bafu.mapping.csv`, remplir `target_product` et
  `target_location` pour ces lignes, et le passer avec `--complement`. Quand
  le statut ajoute `--correspondence may find it`, passer d’abord la table de
  correspondance.
- **`no column … after …`** ou **`two columns …`** : les titres du classeur
  ont changé ; le message nomme la colonne qui manque, ou le titre trouvé deux
  fois, à renommer sur la colonne à ne pas lire.
- **`… inputs find no supplier in …`** : une des versions appelle des
  fournisseurs que ni ecoinvent ni BAFU n’ont ; le message les liste.
- **`references in different units`** ou **`several activities answer to …`**
  (`compare`) : les deux versions n’écrivent pas une activité dans la même
  unité, ou une version a deux activités de même nom, produit et lieu ; le
  script ne choisit pas.
- **`… is written differently in … and …`** (`merge`) : deux bases recopient
  une même activité mais ne l’écrivent pas de la même façon, une base sur
  ecoinvent et sa version BAFU par exemple ; le script ne choisit pas.
- **`VoLCA … is too old`** : il faut un moteur plus récent que la dernière
  version publiée. Chaque lancement télécharge la dernière, le problème se
  règle donc de lui-même à la suivante.
- **`7z extraction failed`** : installer 7-Zip, ou passer un `.zip`.
- **`CERTIFICATE_VERIFY_FAILED` en téléchargeant le moteur** : indiquer à
  Python les certificats que fournit `certifi`, puis relancer :

  ```powershell
  $env:SSL_CERT_FILE = (uv run --with certifi python -c "import certifi; print(certifi.where())")
  ```

- **`Engine exited with code 3221225781`** (Windows) : il manque une DLL au
  moteur ; installer la dernière version de Microsoft Visual C++
  Redistributable x64 (`vc_redist.x64.exe`).

## Les choix inscrits dans le script

La correspondance décide d’abord. Le script porte un choix BAFU pour les neuf
fournisseurs ci-dessous, et ne l’utilise que là où la correspondance ne nomme
aucun procédé BAFU : une ligne qui dit #N/A, un fournisseur nouveau en
ecoinvent 3.12, ou un fournisseur que la correspondance n’a pas.
`--complement` donne d’autres choix à leur place. Ces choix sont des
**propositions, non validées** : chacun devrait être vérifié par quelqu’un qui
connaît les deux bases, et la correspondance corrigée à la source quand c’est
possible.

Avec le classeur du 3 novembre 2026, quatre d’entre eux servent encore : le
sulfate d’alkyle, le produit chimique organique, le polytétrafluoroéthylène
et la bouilloire. La correspondance nomme désormais un procédé pour les cinq
autres, et c’est lui qui sert ; pour le sèche-linge,
c’est « Technical wood drying, infrastructure » {RER}, qui sortait sept fois
plus haut qu’ecoinvent avec le classeur du 28 octobre.

| fournisseur ecoinvent 3.12 | pourquoi une correspondance n’avait rien | choix BAFU | solidité |
|---|---|---|---|
| market for alkyl sulfate (C12-14) {GLO} | la correspondance dit #N/A | Fatty alcohol sulfate, mix, at plant {RER} | proche |
| market for chemical, organic, unspecified {GLO} | nouveau en 3.12, pas de jeu de données 3.11 | Chemicals organic, at plant {GLO} | ce que donne la correspondance pour « chemical, organic » en 3.11 |
| market for polytetrafluoroethylene {GLO} | nouveau en 3.12, pas de jeu de données 3.11 | Tetrafluoroethylene, at plant {RER} | approximation : le monomère, pas le polymère |
| market for dryer {GLO} | la correspondance dit #N/A | Washing machine, at retailer {CH} | approximation faible : BAFU n’a pas de sèche-linge |
| market for electric kettle {GLO} | la correspondance dit #N/A | Convector radiator {CH}, 1,5 kg par appareil, la masse qu’ecoinvent donne à sa bouilloire | approximation faible du fer : BAFU n’a pas de petit électroménager ; c’est l’électricité, pas le fer, qui fait l’essentiel du repassage |
| market for tissue paper {GLO} | la correspondance dit #N/A | Paper, woodfree, uncoated, at regional storage {RER} | proche |
| market for waste mineral oil {RoW} | la correspondance dit #N/A | Disposal, used mineral oil, 10% water, to hazardous waste incineration {CH} | proche |
| market for wastewater from textile production {RoW} | la correspondance dit #N/A | Treatment, sewage, to wastewater treatment, class 3 {CH} | comme le « wastewater, average » de la correspondance |
| market for cationic resin {RoW} | coquille dans la correspondance : « Cationic resin, at plan », sans lieu | Cationic resin, at plant {CH} | l’intention de la correspondance |

Une ligne qui dit « Dummy » n’est pas un manque : la correspondance y marque un
inventaire ecoinvent vide en pratique, sans impact. La ligne qui y mène est
retirée, elle ne compte donc pour rien, et aucun choix ne la remplace. Le
classeur du 3 novembre 2026 le dit du chlorure de magnésium issu de la
production d’éponge de titane, entre autres.

Trois autres fournisseurs ne sont pas des choix mais des renommages : ecoinvent
3.12 découpe par région un marché que la 3.11 avait pour le monde, et la
correspondance ne connaît que ce dernier. Le script les lit comme le fait la
table de correspondance d’ecoinvent, si bien que la correspondance décide pour
eux ; `--correspondence` remplace ces trois-là par la table entière.

| fournisseur ecoinvent 3.12 | lu comme le jeu de données 3.11 |
|---|---|
| market for polyethylene, high density, granulate {RER} | market for polyethylene, high density, granulate {GLO} |
| market for polypropylene, granulate {RER} | market for polypropylene, granulate {GLO} |
| market for polypropylene, granulate {RoW} | market for polypropylene, granulate {GLO} |

## Correspondances à revoir

La correspondance relie les fournisseurs suivants, et Carbonfact les utilise,
mais le procédé BAFU qu’elle nomme est un autre produit ou un autre service. Le
script les applique tels quels ; ils sont listés pour que la correspondance
puisse être améliorée. La liste a été relue à la main contre le classeur du
28 octobre 2026.

Clairement un autre produit :

| fournisseur ecoinvent | procédé BAFU que donne la correspondance |
|---|---|
| market for enzymes {GLO}, enzymes production {RER} | Potato starch, at plant {DE} |
| market for aluminium chloride {GLO} | Aluminium fluoride, at plant {RER} |
| market for citric acid {GLO} | Sulphuric acid, liquid, at plant {RER} |
| market for copper sulfate {GLO} | Copper oxide, at plant {RER} |
| market for cyanuric chloride {GLO} | Hydrogen cyanide, at plant {RER} |
| market for naphthalene sulfonic acid {GLO} | Polyphenylene sulfide, at plant {GLO} |
| market for o-phenylene diamine {GLO} | Polyphenylene sulfide, at plant {GLO} |
| market for polyacrylamide {GLO} | Polylactide, granulate, at plant {GLO} |
| market for sodium amide {RoW} | Sodium chloride, powder, at plant {RER} |
| market for sodium nitrite {RoW} | Sodium dichromate, at plant {RER} |

Approximations à regarder une seconde fois :

| fournisseur ecoinvent | procédé BAFU que donne la correspondance | doute |
|---|---|---|
| market for cooling energy {GLO} | Heat, from natural gas {ENTSO-E}, 1,67 MJ de chaleur par MJ de froid | le froid n’est pas de la chaleur, même si une machine à absorption change bien la chaleur en froid à peu près à ce taux |
| market for dryer {GLO} | Technical wood drying, infrastructure {RER} | une installation de séchage du bois, pas un appareil ménager |
| market for 2-nitroaniline {GLO} | Dinitroaniline-compounds, at regional storehouse {RER} | une famille d’herbicides, pas l’intermédiaire de colorant |
| market for sodium hydrosulfide {GLO} | Hydrogen sulphide, H2S, at plant {RER} | le précurseur, pas le produit |
| market for methacrylic acid {GLO} | Methyl methacrylate, at plant {RER} | l’ester, pas l’acide |
| market for carton board box production, with gravure printing {GLO} | Packaging, corrugated board, mixed fibre, single wall, at plant {RER} | sans impression |
| market for printed paper {GLO} | Paper, newsprint, at plant {CH} | sans impression |
| market for waste packaging paper {RoW} | Waste paper, mixed, from public collection, for further treatment {RER} | une matière collectée, pas un traitement de déchet |
| market for waste polypropylene {RoW} | xx Disposal, polypropylene, 15.9% water, to sanitary landfill {CH} | un nom BAFU qui commence par « xx », à demander à BAFU |
| market for ethanol, … from fermentation {RoW} | Ethanol, 95% in H2O, from sugar cane, at fermentation plant {BR} | une seule matière première et un seul pays pour le reste du monde |

## Vérifier le script

`uv run test_carbonfact_bafu.py` vérifie la lecture du classeur, que la
correspondance décide avant les choix inscrits dans le script, l’appariement
des activités de deux versions et ce qu’une fusion ne garde qu’une fois, sans le moteur.

## Comment il marche

- **Reliaison.** Chaque fournisseur ecoinvent est ramené à la 3.11 (avec la
  table de correspondance, sinon les renommages inscrits dans le script, sinon
  sous son propre nom), cherché dans la
  correspondance et, là où elle n’a pas de procédé BAFU, dans les choix inscrits
  dans le script.
  Deux fournisseurs ecoinvent partagent un même produit et un même lieu
  (« heat, district or industrial, other than natural gas » {RoW}, au
  charbon et au fioul léger) mais vont vers deux procédés BAFU différents ;
  une reliaison repère une entrée par son produit et son lieu, la version BAFU
  nomme donc ses entrées ecoinvent d’après leur activité.
- **Conversion.** La colonne `conversion` de la correspondance dit combien
  d’unités du produit ecoinvent font une unité du procédé BAFU : 2,17 kg d’urée
  par kg de « Urea, as N », 0,599 MJ de froid par MJ de chaleur. Le moteur
  l’applique : 1 kg d’urée devient 0,46 kg de « Urea, as N ». Entre deux unités
  d’une même grandeur et de tailles différentes, comme le kWh et le MJ, la table
  d’unités du moteur fait foi et refuse un facteur qui la contredit. Le script y
  laisse donc le facteur de côté, et affiche chacun de ceux qu’il écarte.
- **Sens.** ecoinvent compte un déchet comme une sortie négative de son marché
  (« market for wastewater from textile production », −1 m³), BAFU comme la
  sortie positive d’un traitement (« Treatment, sewage, … class 3 », +1 m³).
  Une ligne qui garderait le signe d’ecoinvent ferait traiter par BAFU un
  volume négatif, et compter des crédits. Une fois les deux bases chargées, le
  script demande à chacune la quantité de référence de chaque fournisseur et
  de son procédé BAFU, et marque `opposite` les lignes où les signes
  diffèrent ; le moteur retourne ces lignes. Une correspondance exportée de la
  page de correspondance de VoLCA porte déjà cette colonne.
