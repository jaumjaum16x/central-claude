"""Central do Claude.

Junta as novidades do Claude (anuncios da Anthropic, blog do Claude, notas de
versao do app, da API e do Claude Code, pesquisa, engenharia e o que bombou na
comunidade), traduz pro portugues, monta uma pagina (central.html) com linha do
tempo, agenda de datas e dica do dia, e manda as novidades no Telegram.

Uso:  python central_claude.py          -> atualiza e avisa
      python central_claude.py --abrir  -> atualiza e abre a pagina
"""
import email.utils
import hashlib
import html
import json
import os
import re
import sys
import time
import traceback
import urllib.parse
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------- ajustes
CONFIG = {
    "dias_historico": 365,          # quanto tempo a pagina guarda
    "dias_fontes": 120,             # ignora coisas mais velhas que isso nas fontes
    "telegram_pelo_pc": False,      # False quando a nuvem (GitHub) ja estiver mandando
    "telegram_max": 8,              # novidades com mensagem propria por rodada (o resto vai num resumo)
    "telegram_categorias": ["destaque", "app", "code", "api", "noticia", "blog", "pesquisa", "engenharia", "comunidade"],
    "code_itens": 6,                # quantas mudancas de cada versao do Claude Code aparecem no Telegram
    "hn_pontos_min": 300,           # "bombou na comunidade" = post do Hacker News com tantos pontos
    "hora_dica": 9,                 # a dica do dia sai na primeira rodada depois dessa hora
    "lembrete_dias": 7,             # avisa quando faltar isso pra uma data da agenda
    "traduzir": True,
}

PASTA = Path(__file__).resolve().parent
ESTADO = PASTA / os.environ.get("CENTRAL_ESTADO", "estado.json")  # na nuvem fica em dados/
PAGINA = PASTA / "central.html"
LOG = PASTA / "erros.log"
TELEGRAM = PASTA / "telegram.json"  # {"token": "...", "chat_id": ...} - fica so no seu PC
UA = {"User-Agent": "Mozilla/5.0 CentralClaude/1.0"}
AGORA = time.time()

CATS = {  # chave: (nome, emoji, cor)
    "destaque": ("Lancamento grande", "\U0001F6A8", "#ef4444"),
    "app": ("App do Claude", "\U0001F4F1", "#d97757"),
    "code": ("Claude Code", "\U0001F4BB", "#a78bfa"),
    "api": ("API / Plataforma", "\U0001F527", "#38bdf8"),
    "noticia": ("Anuncio Anthropic", "\U0001F4E2", "#f59e0b"),
    "blog": ("Blog do Claude", "\U0001F4DD", "#34d399"),
    "pesquisa": ("Pesquisa", "\U0001F52C", "#f472b6"),
    "engenharia": ("Engenharia", "⚙️", "#94a3b8"),
    "comunidade": ("Na comunidade", "\U0001F525", "#fb923c"),
}

FEEDS = [  # RSS mantidos pela comunidade (github.com/Olshansk/rss-feeds) a partir dos sites oficiais
    ("noticia", "https://raw.githubusercontent.com/Olshansk/rss-feeds/main/feeds/feed_anthropic_news.xml"),
    ("blog", "https://raw.githubusercontent.com/Olshansk/rss-feeds/main/feeds/feed_claude.xml"),
    ("pesquisa", "https://raw.githubusercontent.com/Olshansk/rss-feeds/main/feeds/feed_anthropic_research.xml"),
    ("engenharia", "https://raw.githubusercontent.com/Olshansk/rss-feeds/main/feeds/feed_anthropic_engineering.xml"),
]
NOTAS_API = "https://platform.claude.com/docs/en/release-notes/overview"
NOTAS_APP = "https://support.claude.com/en/articles/12138966-release-notes"
MODELOS = "https://platform.claude.com/docs/en/about-claude/model-deprecations"

# lancamento de modelo ou produto novo vira "destaque"
GRANDE = re.compile(r"\b(introducing|launch(es|ed)?|now available|announcing)\b", re.I)
MODELO_NOVO = re.compile(r"\bclaude (opus|sonnet|haiku|fable|mythos) \d", re.I)


def baixar(url, cabecalhos=None):
    req = urllib.request.Request(url, headers={**UA, **(cabecalhos or {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def log_erro(fonte):
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"[{datetime.now():%d/%m %H:%M}] {fonte}\n{traceback.format_exc()}\n")


def data_en(s):
    """'November 2, 2026' -> timestamp"""
    try:
        return datetime.strptime(s.strip(), "%B %d, %Y").timestamp()
    except ValueError:
        return None


def chave(*partes):
    return hashlib.sha1("|".join(partes).encode()).hexdigest()[:16]


def limpar(txt):
    """Tira markdown/HTML e deixa so o texto."""
    txt = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", txt)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = html.unescape(txt).replace("**", "").replace("`", "")
    return re.sub(r"\s+", " ", txt).strip()


def primeira_frase(txt, limite=140):
    m = re.match(r"(.+?[.!?])(\s|$)", txt)
    f = m.group(1) if m and len(m.group(1)) <= limite else txt[:limite].rsplit(" ", 1)[0] + "..."
    return f if len(txt) > len(f) or f.endswith("...") else txt


def item(cat, k, titulo, url, ts, resumo="", topicos=None, info=""):
    """resumo/topicos vem em ingles e sao traduzidos; info ja e escrito em portugues."""
    if cat in ("noticia", "app") and (GRANDE.search(titulo) or MODELO_NOVO.search(titulo)):
        cat = "destaque"
    return {"k": k, "cat": cat, "titulo": titulo, "url": url, "ts": ts,
            "resumo": resumo, "topicos": topicos or [], "info": info}


# ---------------------------------------------------------------- fontes
def fonte_feeds():
    itens = []
    for cat, url in FEEDS:
        raiz = ET.fromstring(baixar(url))
        for it in raiz.iter("item"):
            titulo = limpar(it.findtext("title") or "")
            link = (it.findtext("link") or "").strip()
            try:
                ts = email.utils.parsedate_to_datetime(it.findtext("pubDate")).timestamp()
            except Exception:
                continue
            if titulo and link:
                itens.append(item(cat, "rss:" + link, titulo, link, ts, limpar(it.findtext("description") or "")[:500]))
    return itens


def fonte_code():
    cab = {"Authorization": f"Bearer {os.environ['GH_TOKEN']}"} if os.environ.get("GH_TOKEN") else {}
    rels = json.loads(baixar("https://api.github.com/repos/anthropics/claude-code/releases?per_page=15", cab))
    itens = []
    for r in rels:
        if r.get("draft") or r.get("prerelease"):
            continue
        topicos = [limpar(l[2:]) for l in (r.get("body") or "").splitlines() if l.startswith(("- ", "* "))]
        ts = datetime.fromisoformat(r["published_at"].replace("Z", "+00:00")).timestamp()
        itens.append(item("code", "code:" + r["tag_name"], f"Claude Code {r['tag_name']}", r["html_url"], ts,
                          topicos=topicos, info=f"{len(topicos)} mudanca(s) nesta versao"))
    return itens


def fonte_api(agenda):
    md = baixar(NOTAS_API + ".md")
    itens = []
    for bloco in re.split(r"^### ", md, flags=re.M)[1:25]:
        cab, _, corpo = bloco.partition("\n")
        ts = data_en(cab)
        if not ts:
            continue
        topicos = [limpar(l[2:]) for l in corpo.splitlines() if l.startswith(("* ", "- "))]
        if not topicos:
            continue
        for t in topicos:  # datas futuras citadas nas notas viram agenda (remocoes, fim de beta...)
            for d in re.findall(r"(?:January|February|March|April|May|June|July|August|September|October|"
                                r"November|December) \d{1,2}, \d{4}", t):
                quando = data_en(d)
                if quando and quando > AGORA - 86400:
                    agenda.append({"k": chave("api", d, t[:80]), "ts": quando, "titulo": primeira_frase(t, 200),
                                   "url": NOTAS_API, "origem": "Notas da API"})
        if len(topicos) == 1:  # uma novidade so: a primeira frase vira o titulo
            titulo = primeira_frase(topicos[0], 220)
            resto = topicos[0] if titulo.endswith("...") else topicos[0][len(titulo):].strip()
            itens.append(item("api", "api:" + cab.strip(), titulo, NOTAS_API, ts, resto))
        else:
            itens.append(item("api", "api:" + cab.strip(), f"{len(topicos)} updates to the API and Console",
                              NOTAS_API, ts, "", topicos))
    return itens


def fonte_app():
    pag = baixar(NOTAS_APP)
    itens = []
    partes = re.split(r"<h3[^>]*>([A-Z][a-z]+ \d{1,2}, \d{4})</h3>", pag)
    for i in range(1, min(len(partes), 60), 2):
        ts = data_en(partes[i])
        if not ts:
            continue
        corpo = partes[i + 1].split("<h2", 1)[0]
        blocos = re.split(r"<p><b>(.*?)</b></p>", corpo)
        for j in range(1, len(blocos), 2):
            titulo = limpar(blocos[j])
            if titulo:
                itens.append(item("app", "app:" + partes[i] + titulo, titulo, NOTAS_APP, ts,
                                  limpar(blocos[j + 1])[:600]))
    return itens


def fonte_comunidade():
    desde = int(AGORA - 7 * 86400)
    url = ("https://hn.algolia.com/api/v1/search?tags=story&query=" + urllib.parse.quote("claude") +
           f"&numericFilters=points>={CONFIG['hn_pontos_min']},created_at_i>{desde}")
    itens = []
    for h in json.loads(baixar(url))["hits"]:
        if not re.search(r"claude|anthropic", h["title"], re.I):
            continue
        link = h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}"
        itens.append(item("comunidade", "hn:" + h["objectID"], h["title"], link, h["created_at_i"],
                          info=f"{h['points']} pontos e {h.get('num_comments') or 0} comentario(s) no Hacker News"))
    return itens


def fonte_modelos(agenda):
    md = baixar(MODELOS + ".md")
    for linha in md.splitlines():
        cols = [c.strip(" `") for c in linha.strip().strip("|").split("|")]
        if len(cols) == 4 and cols[1] == "Deprecated":
            quando = data_en(cols[3])
            ja_tem = any(cols[0] in a["titulo"] and a["ts"] == quando for a in agenda)  # ja veio das notas da API
            if quando and quando > AGORA - 86400 and not ja_tem:
                agenda.append({"k": chave("modelo", cols[0], cols[3]), "ts": quando, "url": MODELOS,
                               "titulo": f"Modelo {cols[0]} sera aposentado (deixa de funcionar na API)",
                               "origem": "Ciclo de vida dos modelos", "pt": True})


# ---------------------------------------------------------------- traducao
def traduzir(textos, cache):
    """Traduz uma lista de textos pro portugues (Google Tradutor gratuito). Falhou = mantem o original."""
    if not CONFIG["traduzir"]:
        return textos
    fora = list(dict.fromkeys(t for t in textos if t and t not in cache))
    lotes, lote = [], []
    for t in fora:  # varios textos numa chamada so, separados por linha (ate ~4500 letras)
        if lote and sum(len(x) for x in lote) + len(t) > 4500:
            lotes.append(lote)
            lote = []
        lote.append(t)
    if lote:
        lotes.append(lote)
    for lote in lotes:
        try:
            saida = google_tradutor("\n".join(t.replace("\n", " ")[:2000] for t in lote)).split("\n")
            if len(saida) != len(lote):  # o tradutor juntou/quebrou linhas: vai um por um
                saida = [google_tradutor(t[:2000]) for t in lote]
            for orig, trad in zip(lote, saida):
                cache[orig] = trad.strip()
        except Exception:
            log_erro("traducao")
            break
    return [cache.get(t, t) for t in textos]


PROTEGER = ["Claude Code", "Claude Console", "Claude Platform", "Claude Duet", "Claude in Chrome", "Anthropic",
            "Sonnet", "Opus", "Haiku", "Fable", "Mythos", "Cowork"]


def google_tradutor(q):
    for i, nome in enumerate(PROTEGER):  # nomes de produto que o tradutor estraga ("Codigo Claude")
        q = q.replace(nome, f"Zx{i}Q")
    req = urllib.request.Request("https://translate.googleapis.com/translate_a/single?client=gtx&sl=en&tl=pt&dt=t",
                                 data=urllib.parse.urlencode({"q": q}).encode(), headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        res = json.load(r)
    saida = "".join(seg[0] for seg in res[0] if seg[0])
    for i, nome in enumerate(PROTEGER):
        saida = re.sub(rf"Zx\s*{i}\s*Q", nome, saida, flags=re.I)
    return saida


def traduzir_itens(estado):
    cache = estado["trad"]
    pendentes = [it for it in list(estado["itens"].values()) + list(estado["agenda"].values()) if not it.get("pt")]
    textos = lambda it: [it["titulo"], it.get("resumo", "")] + it.get("topicos", [])[:12]
    traduzir([t for it in pendentes for t in textos(it)], cache)  # tudo de uma vez, em lotes
    for it in pendentes:
        orig = textos(it)
        trad = [cache.get(t, t) for t in orig]
        it["titulo_pt"], it["resumo_pt"] = trad[0], trad[1]
        if "topicos" in it:
            it["topicos_pt"] = trad[2:] + it["topicos"][12:]
        it["pt"] = all(t in cache or not t for t in orig)
    usados = {t for it in list(estado["itens"].values()) + list(estado["agenda"].values()) for t in textos(it)}
    estado["trad"] = {k: v for k, v in cache.items() if k in usados}


# ---------------------------------------------------------------- estado
def carregar_estado():
    try:
        e = json.loads(ESTADO.read_text(encoding="utf-8"))
    except Exception:
        e = {}
    for k, v in (("itens", {}), ("agenda", {}), ("trad", {}), ("lembrados", []), ("dica_dia", "")):
        e.setdefault(k, v)
    return e


def processar(itens, agenda, estado):
    """Junta com o historico e devolve so o que e novo."""
    primeira_vez = not estado["itens"]
    limite = AGORA - CONFIG["dias_fontes"] * 86400
    vistos_titulo = {re.sub(r"\W+", "", i["titulo"].lower()) for i in estado["itens"].values()}
    novos = []
    for it in sorted(itens, key=lambda i: i["ts"]):
        if it["ts"] < limite:
            continue
        if it["k"] in estado["itens"]:
            antigo = estado["itens"][it["k"]]
            if antigo["topicos"] != it["topicos"] or antigo["resumo"] != it["resumo"]:
                it["visto"] = antigo.get("visto", AGORA)
                estado["itens"][it["k"]] = it  # mesma data ganhou mais coisa: atualiza sem avisar de novo
            continue
        t = re.sub(r"\W+", "", it["titulo"].lower())
        if t in vistos_titulo:  # mesma materia em dois feeds (noticia e pesquisa)
            continue
        vistos_titulo.add(t)
        it["visto"] = it["ts"] if primeira_vez else AGORA  # selo "novo" so pro que saiu de verdade agora
        estado["itens"][it["k"]] = it
        novos.append(it)
    corte = AGORA - CONFIG["dias_historico"] * 86400
    estado["itens"] = {k: v for k, v in estado["itens"].items() if v["ts"] >= corte}

    novas_datas = []
    for a in agenda:
        if a["k"] not in estado["agenda"]:
            estado["agenda"][a["k"]] = a
            novas_datas.append(a)
    estado["agenda"] = {k: v for k, v in estado["agenda"].items() if v["ts"] > AGORA - 3 * 86400}
    estado["lembrados"] = [k for k in estado["lembrados"] if k in estado["agenda"]]
    return novos, novas_datas, primeira_vez


# ---------------------------------------------------------------- dicas
DICAS = [
    ("Comece pelo contexto", "Quanto mais contexto voce da, melhor o Claude responde: pra quem e, qual o objetivo, o que ja tentou e como quer o resultado. Pense nele como um colega novo e muito esperto que ainda nao conhece seu projeto."),
    ("Peca pra ele perguntar antes", "Em tarefas grandes, termine o pedido com 'antes de comecar, me faca as perguntas que precisar'. Ele tira as duvidas e erra bem menos."),
    ("Mostre exemplos", "Se voce quer um formato ou estilo especifico, cole um ou dois exemplos. Exemplo vale mais que explicacao."),
    ("Diga o que fazer, nao so o que evitar", "'Responda em 3 frases curtas' funciona melhor que 'nao seja prolixo'."),
    ("Use tags pra separar as partes", "Em pedidos longos, separe as partes com tags tipo <documento>...</documento> e <tarefa>...</tarefa>. O Claude entende melhor o que e material e o que e instrucao."),
    ("Projetos no claude.ai", "Num Projeto voce sobe arquivos e escreve instrucoes uma vez so, e todas as conversas dentro dele ja sabem daquilo. Otimo pra estudos, trabalho ou um app que voce esta construindo."),
    ("Artefatos", "Peca uma pagina, um app pequeno, um grafico ou um documento e o Claude cria um artefato ao lado da conversa, que voce pode testar, editar e compartilhar."),
    ("Escolha o modelo certo", "Haiku e o mais rapido e barato, Sonnet e o equilibrado do dia a dia e Opus e o mais capaz pra tarefas dificeis. Troque conforme o tamanho do problema."),
    ("Faca ele revisar o proprio trabalho", "Depois de uma resposta importante, peca: 'revise isso procurando erros e pontos fracos'. Ele costuma achar e corrigir coisas sozinho."),
    ("Divida tarefas grandes", "Em vez de um pedido gigante, va em etapas: primeiro o plano, depois cada parte. Fica mais facil corrigir o rumo."),
    ("Pesquisa na web", "Quando o assunto e recente (preco, noticia, versao nova), ative a busca na web ou o modo Pesquisa pra ele consultar fontes atuais e citar de onde tirou."),
    ("Memoria do Claude", "O app do Claude pode lembrar de coisas suas entre conversas. Da pra ver e editar o que ele guardou nas configuracoes."),
    ("Claude Code: o CLAUDE.md", "No Claude Code, o arquivo CLAUDE.md guarda as regras do projeto (como rodar, padroes, cuidados). Ele e lido em toda conversa. O comando /init cria um pra voce."),
    ("Claude Code: modo plano", "Aperte Shift+Tab pra alternar os modos de permissao. No modo plano o Claude so investiga e propoe um plano, sem mexer em nada, ate voce aprovar."),
    ("Claude Code: /compact e /clear", "Conversa ficou enorme? /compact resume o historico e libera espaco. Mudou de assunto? /clear comeca do zero. As duas coisas deixam ele mais rapido e preciso."),
    ("Claude Code: mencione arquivos com @", "Digite @ e o nome do arquivo pra colocar ele direto no contexto, sem o Claude precisar procurar."),
    ("Claude Code: comando direto com !", "Comece a mensagem com ! pra rodar um comando no terminal na hora, e o resultado ja fica na conversa pro Claude ver."),
    ("Claude Code: Esc interrompe", "Aperte Esc pra parar o Claude no meio quando ele estiver indo pelo caminho errado. Esc duas vezes volta pra uma mensagem anterior e deixa voce refazer dali."),
    ("Claude Code: cole imagens", "Da pra colar um print direto no terminal (Ctrl+V). Otimo pra mostrar um erro na tela ou o visual que voce quer."),
    ("Claude Code: retomar conversa", "'claude --continue' volta pra ultima conversa e 'claude --resume' deixa escolher uma antiga. Nada se perde quando voce fecha o terminal."),
    ("Claude Code: modo sem conversa", "'claude -p \"sua pergunta\"' responde e sai, sem abrir a conversa. Da pra usar em scripts e tarefas agendadas."),
    ("Claude Code: troque de modelo", "O comando /model troca o modelo no meio da conversa. Use o mais forte pra planejar e um mais rapido pra tarefas simples."),
    ("Claude Code: comandos proprios", "Crie arquivos .md em .claude/commands e eles viram comandos com barra. Bom pra aquele pedido que voce repete sempre."),
    ("Claude Code: Skills", "Skills sao pastas com um SKILL.md ensinando o Claude a fazer uma tarefa do seu jeito (um relatorio, um deploy, um padrao de codigo). Ele carrega sozinho quando o assunto aparece."),
    ("Claude Code: subagentes", "Subagentes sao ajudantes com instrucoes e ferramentas proprias, definidos em .claude/agents. Bons pra tarefas paralelas sem encher a conversa principal."),
    ("Claude Code: hooks", "Hooks rodam comandos automaticos em momentos certos, tipo formatar o codigo depois de cada edicao ou tocar um som quando ele termina. Ficam no settings.json."),
    ("Claude Code: MCP", "Com 'claude mcp add' voce conecta o Claude a outros servicos (GitHub, banco de dados, navegador, Notion...) e ele passa a usar essas ferramentas."),
    ("Claude Code: menos pedidos de permissao", "Use /permissions pra liberar de vez os comandos que voce sempre aprova, tipo rodar testes. Economiza um monte de cliques."),
    ("Claude Code: pense mais", "Pra problema cabeludo, peca pra ele pensar com calma ('think hard' ou 'ultrathink'). Ele gasta mais raciocinio antes de responder."),
    ("Claude Code: tarefas repetidas", "O /loop repete uma tarefa de tempos em tempos (tipo checar um deploy) e o /schedule cria rotinas que rodam na nuvem num horario."),
    ("Claude Code: revisao de codigo", "Antes de subir uma mudanca, rode /code-review pra ele procurar bugs no que foi alterado."),
    ("Claude Code: mostre como testar", "Diga ao Claude como verificar o trabalho (rodar os testes, abrir a pagina, comparar com um print). Com um jeito de conferir, ele itera ate acertar."),
    ("Claude Code: em todo lugar", "O Claude Code roda no terminal, no app de desktop, na web (claude.ai/code) e dentro do VS Code e JetBrains. A mesma conta serve pra todos."),
    ("Claude Code: memoria do projeto", "Quando ele aprender algo importante sobre o projeto, peca pra anotar no CLAUDE.md. Na proxima conversa ele ja comeca sabendo."),
    ("API: cache de prompt", "Na API, o cache de prompt reaproveita partes repetidas (instrucoes longas, documentos) e corta bastante o custo e o tempo de resposta."),
    ("API: lote com desconto", "Se a resposta nao precisa ser na hora, a Batch API processa muitos pedidos de uma vez pela metade do preco."),
    ("API: respostas estruturadas", "Pra integrar com codigo, peca a saida em JSON com um formato definido (ou use ferramentas/tool use). Fica facil de ler no seu programa."),
    ("Claude no navegador", "A extensao Claude in Chrome deixa o Claude ver e mexer nas paginas que voce esta usando: preencher formulario, comparar produtos, resumir uma pagina."),
    ("Peca o ponto de vista contrario", "Pra decisoes, peca: 'agora argumente contra isso'. Voce enxerga os riscos antes de decidir."),
    ("Edite a pergunta em vez de discutir", "Se a resposta veio torta, muitas vezes e melhor editar sua mensagem e reenviar com mais detalhe do que ficar corrigindo em varias mensagens."),
]


def dica_de_hoje():
    n = (datetime.now().date() - datetime(2026, 1, 1).date()).days
    return DICAS[n % len(DICAS)]


# ---------------------------------------------------------------- telegram
def esc(s):
    return html.escape(s or "", quote=False)


def data_br(ts, hora=False):
    d = datetime.fromtimestamp(ts)
    return d.strftime("%d/%m/%Y %H:%M" if hora else "%d/%m/%Y")


def faltam(ts):
    dias = (datetime.fromtimestamp(ts).date() - datetime.now().date()).days
    return "hoje" if dias == 0 else "amanha" if dias == 1 else f"em {dias} dias" if dias > 0 else "ja passou"


def texto_item(it):
    nome, emoji, _ = CATS[it["cat"]]
    linhas = [f"{emoji} <b>{nome.upper()}</b> · {data_br(it['ts'])}", f"<b>{esc(it.get('titulo_pt') or it['titulo'])}</b>"]
    if it.get("resumo_pt"):
        linhas.append(esc(it["resumo_pt"][:450]))
    if it.get("info"):
        linhas.append(f"<i>{esc(it['info'])}</i>")
    tops = it.get("topicos_pt") or it["topicos"]
    if tops:
        n = CONFIG["code_itens"] if it["cat"] == "code" else 8
        linhas.append("\n".join(f"• {esc(t[:300])}" for t in tops[:n]))
        if len(tops) > n:
            linhas.append(f"<i>+{len(tops) - n} mudanca(s) no link</i>")
    return "\n\n".join(linhas)[:4000]


def botoes(url=None):
    linha = []
    if url:
        linha.append({"text": "\U0001F517 Abrir", "url": url})
    if os.environ.get("PAGINA_URL"):
        linha.append({"text": "\U0001F310 Central do Claude", "url": os.environ["PAGINA_URL"]})
    return {"inline_keyboard": [linha]} if linha else None


def config_telegram():
    if os.environ.get("TELEGRAM_TOKEN"):  # na nuvem: vem do cofre secreto do GitHub
        return os.environ["TELEGRAM_TOKEN"], os.environ.get("TELEGRAM_CHAT_ID")
    if not CONFIG["telegram_pelo_pc"] and "--telegram" not in sys.argv:
        return None, None  # a nuvem ja manda; evita mensagem repetida
    try:
        cfg = json.loads(TELEGRAM.read_text(encoding="utf-8"))
        return cfg.get("token"), cfg.get("chat_id")
    except Exception:
        return None, None


def mandar(token, chat, texto, url=None):
    dados = {"chat_id": chat, "text": texto, "parse_mode": "HTML", "disable_web_page_preview": True}
    if botoes(url):
        dados["reply_markup"] = botoes(url)
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",
                                 data=json.dumps(dados).encode(), headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=20).read()
    time.sleep(1.1)  # o Telegram limita mensagens seguidas


def enviar_telegram(estado, novos, novas_datas, primeira_vez):
    token, chat = config_telegram()
    if not token or not chat:
        return
    if primeira_vez:  # nao despeja o historico inteiro: manda um resumo de boas-vindas
        ultimos = sorted(estado["itens"].values(), key=lambda i: -i["ts"])[:10]
        txt = ("\U0001F916 <b>Central do Claude ligada!</b>\n\nA partir de agora eu te aviso aqui de tudo que sair: "
               "versoes novas, funcoes novas do app, Claude Code, API, anuncios, pesquisa, datas importantes "
               "e uma dica por dia.\n\n<b>Ultimas novidades:</b>\n" +
               "\n".join(f"{CATS[i['cat']][1]} {data_br(i['ts'])[:5]} — {esc(i.get('titulo_pt') or i['titulo'])}" for i in ultimos))
        mandar(token, chat, txt[:4000])
        novos = []
    else:
        novos = [i for i in novos if i["cat"] in CONFIG["telegram_categorias"]]
        peso = {c: n for n, c in enumerate(CATS)}
        novos.sort(key=lambda i: (peso[i["cat"]], -i["ts"]))
        for it in novos[:CONFIG["telegram_max"]]:
            mandar(token, chat, texto_item(it), it["url"])
        resto = novos[CONFIG["telegram_max"]:]
        if resto:
            mandar(token, chat, f"\U0001F4CB <b>Mais {len(resto)} novidade(s)</b>\n\n" +
                   "\n".join(f"{CATS[i['cat']][1]} <a href=\"{esc(i['url'])}\">{esc(i.get('titulo_pt') or i['titulo'])}</a>"
                             for i in resto)[:3800])

    # agenda: data nova anunciada e lembrete quando estiver chegando
    futuras = sorted(estado["agenda"].values(), key=lambda a: a["ts"])
    novas_k = {a["k"] for a in novas_datas}
    if primeira_vez and futuras:
        mandar(token, chat, "\U0001F4C5 <b>Agenda do Claude</b>\n\n" + "\n".join(
            f"<b>{data_br(a['ts'])}</b> ({faltam(a['ts'])}) — {esc(a.get('titulo_pt') or a['titulo'])}" for a in futuras[:10]))
    elif novas_datas:
        mandar(token, chat, "\U0001F4C5 <b>Data nova na agenda</b>\n\n" + "\n".join(
            f"<b>{data_br(a['ts'])}</b> ({faltam(a['ts'])}) — {esc(a.get('titulo_pt') or a['titulo'])}" for a in novas_datas))
    perto = [a for a in futuras if a["k"] not in estado["lembrados"] and a["k"] not in novas_k
             and 0 <= a["ts"] - AGORA <= CONFIG["lembrete_dias"] * 86400]
    if perto and not primeira_vez:
        mandar(token, chat, "⏰ <b>Lembrete: esta chegando</b>\n\n" + "\n".join(
            f"<b>{data_br(a['ts'])}</b> ({faltam(a['ts'])}) — {esc(a.get('titulo_pt') or a['titulo'])}" for a in perto))
    estado["lembrados"] += [a["k"] for a in futuras if 0 <= a["ts"] - AGORA <= CONFIG["lembrete_dias"] * 86400]

    hoje = datetime.now().strftime("%Y-%m-%d")
    if estado["dica_dia"] != hoje and datetime.now().hour >= CONFIG["hora_dica"]:
        titulo, texto = dica_de_hoje()
        mandar(token, chat, f"\U0001F4A1 <b>Dica do dia: {esc(titulo)}</b>\n\n{esc(texto)}")
        estado["dica_dia"] = hoje


# ---------------------------------------------------------------- pagina
DIAS = ["segunda", "terca", "quarta", "quinta", "sexta", "sabado", "domingo"]


def card(it):
    nome, emoji, cor = CATS[it["cat"]]
    tops = it.get("topicos_pt") or it["topicos"]
    lista = "".join(f"<li>{esc(t)}</li>" for t in tops[:30])
    mais = f"<li class='apagado'>+{len(tops) - 30} no link</li>" if len(tops) > 30 else ""
    original = it["titulo"] if it.get("titulo_pt") and it["titulo_pt"] != it["titulo"] else ""
    novo = "<span class='novo'>novo</span>" if AGORA - it.get("visto", 0) < 2 * 86400 else ""
    busca = attr(" ".join([it["titulo"], it.get("titulo_pt", ""), it["resumo"], it.get("resumo_pt", "")] + tops).lower())
    acoes = (f'<div class="acoes"><a class="btn ia" href="{attr(link_explica(it))}" target="_blank" rel="noopener" '
             f'title="Abre o Claude ja com a pergunta pronta sobre essa novidade">✨ Me explica</a>'
             f'{botoes_busca(it)}<a class="btn" href="{attr(it["url"])}" target="_blank" rel="noopener">\U0001F517 Fonte</a></div>')
    return (f'<article class="card" data-cat="{it["cat"]}" data-busca="{busca}" style="--c:{cor}">'
            f'<div class="topo"><span class="cat">{emoji} {nome}</span>{novo}<time>{data_br(it["ts"])}</time></div>'
            f'<h3><a href="{esc(it["url"])}" target="_blank" rel="noopener">{esc(it.get("titulo_pt") or it["titulo"])}</a></h3>'
            + (f'<small class="orig">{esc(original)}</small>' if original else "")
            + (f'<p>{esc(it.get("resumo_pt") or it["resumo"])}</p>' if it["resumo"] else "")
            + (f'<p class="info">{esc(it["info"])}</p>' if it.get("info") else "")
            + (f'<details{" open" if len(tops) <= 4 else ""}><summary>{len(tops)} item(ns)</summary><ul>{lista}{mais}</ul></details>' if tops else "")
            + acoes + "</article>")


VAZIAS = re.compile(r"^(apresentando|lan[cç]amento d[eoa]s?|lan[cç]amos|anunciando|anunciamos|novidade:?|agora,?)\s+", re.I)


def termo_busca(it):
    """Assunto curto em portugues pra buscar noticias e videos (titulo inteiro acha pouca coisa)."""
    if it["cat"] == "code":
        return "Claude Code novidades atualizacao"
    t = VAZIAS.sub("", it.get("titulo_pt") or it["titulo"])
    palavras = re.sub(r"[^\w\s.-]", " ", t).split()[:9]
    while len(palavras) > 2 and palavras[-1].lower() in ("e", "com", "de", "da", "do", "das", "dos", "no", "na", "para", "pra", "em", "a", "o", "os", "as", "um", "uma"):
        palavras.pop()
    t = " ".join(palavras)
    return t if re.search(r"claude|anthropic", t, re.I) else f"Claude {t}"


def botoes_busca(it):
    q = urllib.parse.quote(termo_busca(it))
    return (f'<a class="btn" href="https://www.google.com/search?q={q}&tbm=nws&hl=pt-BR&gl=BR" target="_blank" '
            f'rel="noopener" title="Noticias em portugues sobre isso">\U0001F4F0 Noticias</a>'
            f'<a class="btn" href="https://www.youtube.com/results?search_query={q}" target="_blank" '
            f'rel="noopener" title="Videos sobre isso no YouTube">▶️ Videos</a>')


def attr(s):
    return html.escape(s or "", quote=True)


def link_explica(it):
    """Abre o claude.ai com a pergunta pronta sobre a novidade."""
    detalhes = it["resumo"] or "\n".join(f"- {t}" for t in it["topicos"][:10])
    pergunta = (f"Me explica em portugues, de um jeito simples, essa novidade do Claude ({data_br(it['ts'])}):\n\n"
                f"\"{it['titulo']}\"\n{detalhes[:900]}\nFonte: {it['url']}\n\n"
                "Quero entender: o que e, pra que serve, o que muda pra mim como usuario e um exemplo pratico de como usar.")
    return "https://claude.ai/new?q=" + urllib.parse.quote(pergunta[:1800])


# termos que ganham explicacao ao passar o mouse (e aparecem no glossario)
GLOSSARIO = [
    ("Claude Code", r"Claude Code", "Ferramenta do Claude pra programar: le, edita e roda codigo no seu computador, pelo terminal, app de desktop, web ou dentro do editor."),
    ("Opus", r"\bOpus\b", "O modelo mais capaz da familia Claude, pra tarefas dificeis. E o mais caro e um pouco mais lento."),
    ("Sonnet", r"\bSonnet\b", "O modelo equilibrado do Claude: muito bom, rapido e com preco medio. O do dia a dia."),
    ("Haiku", r"\bHaiku\b", "O modelo mais rapido e barato do Claude, bom pra tarefas simples e em grande quantidade."),
    ("API", r"\bAPI\b", "O jeito de programas usarem o Claude por codigo, pagando pelo uso (em tokens). E o que os apps e sites usam por tras."),
    ("Console", r"\bConsole\b", "Site dos desenvolvedores do Claude: cria chaves da API, mostra custos e deixa testar prompts."),
    ("Tokens", r"\btokens?\b", "Pedacinhos de texto (mais ou menos 3/4 de uma palavra). Precos e limites da API sao medidos em tokens."),
    ("Contexto", r"\b(janela de contexto|contexto|context window)\b", "Quanto texto o Claude consegue considerar de uma vez: a conversa, os arquivos e as instrucoes juntos."),
    ("MCP", r"\bMCP\b", "Model Context Protocol: padrao aberto que conecta o Claude a outros apps e servicos (GitHub, Google Drive, bancos de dados...)."),
    ("Skills", r"\b(skills?|habilidades)\b", "Pacotes de instrucoes e arquivos que ensinam o Claude a fazer uma tarefa do seu jeito. Ele carrega sozinho quando precisa."),
    ("Subagentes", r"\bsub-?agentes?\b|\bsubagents?\b", "Ajudantes que o Claude cria pra fazer partes do trabalho em paralelo, cada um com seu proprio contexto."),
    ("Agente", r"\bagentes?\b|\bagents?\b|\bag[eê]ntic[oa]s?\b", "Quando o Claude executa uma tarefa em varias etapas sozinho, usando ferramentas, em vez de so responder uma pergunta."),
    ("Hooks", r"\bhooks?\b", "Comandos que rodam automaticamente em certos momentos do Claude Code, tipo formatar o codigo depois de cada edicao."),
    ("Plugins", r"\bplugins?\b", "Pacotes que juntam comandos, skills, agentes e conexoes MCP pra instalar no Claude Code de uma vez."),
    ("Artefatos", r"\bartefatos?\b|\bartifacts?\b", "Conteudo que o Claude cria ao lado da conversa (pagina, app, documento, grafico) e que da pra ver, editar e compartilhar."),
    ("Projetos", r"\bProjetos\b|\bProjects\b", "Espacos no claude.ai com arquivos e instrucoes que valem pra todas as conversas dentro deles."),
    ("Beta", r"\bbeta\b", "Recurso novo ainda em teste. Pode mudar e as vezes precisa ser ativado na mao."),
    ("Previa de pesquisa", r"pr[eé]via de pesquisa|research preview", "Lancamento experimental pra um grupo testar antes da versao final. Pode mudar bastante."),
    ("Descontinuado", r"\bdescontinua\w*|\bdeprecat\w*|\bobsolet\w*", "Ainda funciona, mas ja tem data pra ser desligado. Hora de trocar pelo substituto."),
    ("Aposentado", r"\baposentad\w*|\bretired\b", "Ja foi desligado: pedidos pra ele dao erro."),
    ("Cache de prompt", r"cache de prompt|prompt caching", "Reaproveita partes repetidas de um pedido na API (instrucoes longas, documentos), deixando mais barato e mais rapido."),
    ("Batch API", r"\bBatch\b", "Manda muitos pedidos de uma vez pra API, sem pressa de resposta, pela metade do preco."),
    ("Modo rapido", r"modo r[aá]pido|fast mode", "Modo que entrega as respostas mais rapido, cobrando mais caro por isso."),
    ("Raciocinio", r"\bracioc[ií]nio\b|pensamento estendido|extended thinking|\bultrathink\b", "O Claude pensa passo a passo antes de responder. Demora mais, mas acerta mais em problemas dificeis."),
    ("Ferramentas", r"\buso de ferramentas\b|\btool use\b", "Quando o Claude chama funcoes externas (buscar na web, rodar codigo, consultar um sistema) pra completar a tarefa."),
    ("SDK", r"\bSDKs?\b", "Bibliotecas oficiais pra usar a API do Claude em Python, TypeScript e outras linguagens."),
    ("CLAUDE.md", r"CLAUDE\.md", "Arquivo de regras do projeto no Claude Code. Ele e lido no comeco de toda conversa."),
    ("Claude in Chrome", r"Claude in Chrome|Claude no Chrome", "Extensao do navegador que deixa o Claude ver e mexer nas paginas que voce esta usando."),
    ("Cowork", r"\bCowork\b", "Modo do app de desktop do Claude em que ele trabalha como agente nos seus arquivos e tarefas do dia a dia, nao so em codigo."),
    ("Nuvens parceiras", r"\bBedrock\b|\bVertex\b|\bFoundry\b", "Nuvens da Amazon (Bedrock), Google (Vertex) e Microsoft (Foundry), onde empresas tambem podem usar o Claude."),
    ("Planos", r"\b(Pro|Max|Team|Enterprise)\b(?= (e|and|plan|plano|users|usu))|planos? (Pro|Max|Team|Enterprise)", "Planos pagos do Claude: Pro (individual), Max (uso pesado), Team (equipes) e Enterprise (empresas)."),
]


def destaque_grande(it):
    nome, emoji, cor = CATS[it["cat"]]
    resumo = it.get("resumo_pt") or it["resumo"] or " · ".join((it.get("topicos_pt") or it["topicos"])[:3])
    return (f'<article class="hero-card card" data-cat="{it["cat"]}" data-busca="" style="--c:{cor}">'
            f'<div class="topo"><span class="cat">{emoji} {nome}</span><time>{data_br(it["ts"])}</time></div>'
            f'<h3><a href="{attr(it["url"])}" target="_blank" rel="noopener">{esc(it.get("titulo_pt") or it["titulo"])}</a></h3>'
            f'<p>{esc(resumo[:260])}</p>'
            f'<div class="acoes"><a class="btn ia" href="{attr(link_explica(it))}" target="_blank" rel="noopener">✨ Me explica</a>'
            f'{botoes_busca(it)}</div></article>')


def gerar_pagina(estado, erros):
    itens = sorted(estado["itens"].values(), key=lambda i: -i["ts"])
    contagem = {c: sum(1 for i in itens if i["cat"] == c) for c in CATS}
    filtros = "".join(f'<button data-cat="{c}" style="--c:{v[2]}">{v[1]} {v[0]} <i>{contagem[c]}</i></button>'
                      for c, v in CATS.items() if contagem[c])
    linhas, dia_atual = [], None
    for it in itens:
        d = datetime.fromtimestamp(it["ts"])
        if d.date() != dia_atual:
            if dia_atual:
                linhas.append("</div></section>")
            dia_atual = d.date()
            linhas.append(f'<section class="dia"><h2>{d:%d/%m/%Y} <span>{DIAS[d.weekday()]}</span></h2><div class="lista">')
        linhas.append(card(it))
    if dia_atual:
        linhas.append("</div></section>")

    agenda = sorted(estado["agenda"].values(), key=lambda a: a["ts"])
    ag = "".join(f'<li><b>{data_br(a["ts"])}</b> <em class="cd" data-ts="{a["ts"]}">{faltam(a["ts"])}</em>'
                 f'<a href="{attr(a["url"])}" target="_blank" rel="noopener">{esc(a.get("titulo_pt") or a["titulo"])}</a>'
                 f'<small>{esc(a["origem"])}</small></li>' for a in agenda) or "<li class='apagado'>Nada marcado por enquanto.</li>"
    titulo_dica, texto_dica = dica_de_hoje()
    todas = "".join(f"<li><b>{esc(t)}</b> {esc(x)}</li>" for t, x in DICAS)
    termos = "".join(f'<div class="termo-card"><b>{esc(n)}</b><p>{esc(d)}</p></div>' for n, _, d in GLOSSARIO)
    sensiveis = {"Planos", "Console", "API", "SDK", "MCP", "Batch API", "Opus", "Sonnet", "Haiku", "Cowork"}
    glossario_js = json.dumps([[n, r, d, "" if n in sensiveis else "i"] for n, r, d in GLOSSARIO], ensure_ascii=False)
    aviso_erro = f'<p class="erro">Nao consegui ler: {", ".join(erros)} (o resto esta atualizado)</p>' if erros else ""

    # numeros do painel
    dias = lambda n: AGORA - n * 86400
    semana = sum(1 for i in itens if i["ts"] >= dias(7))
    versoes = sum(1 for i in itens if i["cat"] == "code" and i["ts"] >= dias(30))
    grandes = sum(1 for i in itens if i["cat"] == "destaque" and i["ts"] >= dias(90))
    prox = agenda[0] if agenda else None
    prox_txt = f'{data_br(prox["ts"])[:5]}<small>{faltam(prox["ts"])}</small>' if prox else "—<small>nada marcado</small>"
    destaques = [i for i in itens if i["cat"] == "destaque" and i["ts"] >= dias(120)][:4]
    hero = (f'<h2 class="titulo-sec">\U0001F6A8 Lancamentos grandes</h2><div class="hero">{"".join(destaque_grande(i) for i in destaques)}</div>'
            if destaques else "")

    PAGINA.write_text(f"""<!doctype html><html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Central do Claude</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='16' fill='%23d97757'/%3E%3Cpath d='M32 10C34 25 39 30 54 32C39 34 34 39 32 54C30 39 25 34 10 32C25 30 30 25 32 10Z' fill='%23fff7f0'/%3E%3C/svg%3E">
<style>
body{{margin:0;font-family:Segoe UI,system-ui,sans-serif;background:#0d0b0a;color:#ece6e1}}
body::before{{content:"";position:fixed;inset:-20%;z-index:-1;pointer-events:none;filter:blur(50px);
  background:radial-gradient(35% 30% at 15% 20%,#d977574d,transparent 70%),radial-gradient(30% 30% at 85% 15%,#a78bfa33,transparent 70%),
  radial-gradient(35% 35% at 60% 85%,#38bdf826,transparent 70%);animation:au 50s ease-in-out infinite alternate}}
body::after{{content:"";position:fixed;inset:0;z-index:-1;pointer-events:none;
  background-image:linear-gradient(#ffffff0d 1px,transparent 1px),linear-gradient(90deg,#ffffff0d 1px,transparent 1px);background-size:44px 44px;
  -webkit-mask-image:radial-gradient(ellipse 90% 60% at 50% 0%,#000 25%,transparent 75%);mask-image:radial-gradient(ellipse 90% 60% at 50% 0%,#000 25%,transparent 75%)}}
@keyframes au{{to{{transform:translate(4%,-3%) rotate(8deg) scale(1.06)}}}}
@media (prefers-reduced-motion:reduce){{body::before{{animation:none}}}}
a{{color:inherit}}
header{{position:sticky;top:0;z-index:3;background:#14110fcc;backdrop-filter:blur(10px);padding:12px 24px;display:flex;gap:14px;align-items:center;border-bottom:1px solid #2b2420;flex-wrap:wrap}}
.logo{{width:36px;height:36px;border-radius:10px;background:linear-gradient(135deg,#f4a27c,#d97757 55%,#b4532f);display:grid;place-items:center;box-shadow:0 4px 16px #d9775766}}
header h1{{font-size:20px;margin:0}} header h1 b{{color:#d97757}} header small{{color:#9b8f86;display:block;font-size:12px}}
.busca{{margin-left:auto;display:flex;gap:8px}}
input{{background:#1a1613;border:1px solid #3a302a;color:#ece6e1;padding:9px 12px;border-radius:10px;width:280px}}
input:focus{{outline:none;border-color:#d97757}}
.btn{{display:inline-flex;align-items:center;gap:4px;background:#221c18;border:1px solid #3a302a;color:#e2d8d0;padding:6px 11px;border-radius:999px;font-size:12px;text-decoration:none;cursor:pointer;transition:.15s}}
.btn:hover{{border-color:#d97757;color:#fff}}
.btn.ia{{background:linear-gradient(135deg,#d97757,#b4532f);border-color:transparent;color:#fff;font-weight:600}}
.btn.ia:hover{{filter:brightness(1.12);box-shadow:0 4px 14px #d9775766}}
#perguntar{{padding:9px 14px;font-size:13px;border-radius:10px}}
.stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;padding:16px 24px 4px}}
.stat{{background:#18141199;border:1px solid #2f2722;border-radius:14px;padding:12px 16px}}
.stat b{{font-size:28px;display:block;background:linear-gradient(135deg,#ffd2bd,#d97757);-webkit-background-clip:text;background-clip:text;color:transparent}}
.stat b small{{font-size:13px;margin-left:6px;-webkit-text-fill-color:#fbbf24}} .stat span{{color:#9b8f86;font-size:12px}}
.titulo-sec{{font-size:15px;margin:18px 24px 10px;color:#ece6e1}}
.hero{{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:14px;padding:0 24px}}
.hero-card{{background:radial-gradient(120% 120% at 0% 0%,color-mix(in srgb,var(--c) 28%,transparent),#18141199 60%)!important;border-radius:16px!important;padding:16px 18px!important}}
.hero-card h3{{font-size:18px!important}}
.filtros{{position:sticky;top:61px;z-index:2;background:#0d0b0ab8;backdrop-filter:blur(10px);padding:10px 24px;display:flex;gap:6px;flex-wrap:wrap;border-bottom:1px solid #2b2420;margin-top:14px}}
.filtros button{{background:#1a1613;border:1px solid #3a302a;color:#d9d0c9;padding:5px 11px;border-radius:999px;cursor:pointer;font-size:13px}}
.filtros button.on{{background:var(--c,#d97757);border-color:var(--c,#d97757);color:#111}} .filtros i{{font-style:normal;opacity:.7;font-size:11px}}
.painel{{display:grid;grid-template-columns:1fr 1fr;gap:14px;padding:14px 24px 0}}
@media (max-width:900px){{.painel{{grid-template-columns:1fr}} .stats{{grid-template-columns:1fr 1fr}} input{{width:100%}} .busca{{width:100%}}}}
.box{{background:#18141199;border:1px solid #2f2722;border-radius:14px;padding:14px 18px}}
.box h2{{font-size:15px;margin:0 0 10px;color:#d97757}}
.dica b{{display:block;font-size:17px;margin-bottom:6px}} .dica p{{margin:0;line-height:1.5;color:#d9d0c9}}
.agenda ul{{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:8px;max-height:220px;overflow:auto}}
.agenda li{{display:grid;grid-template-columns:auto auto 1fr;gap:4px 10px;align-items:baseline;font-size:13px}}
.agenda li a{{text-decoration:none}} .agenda li a:hover{{text-decoration:underline}} .agenda small{{grid-column:3;color:#7d726a;font-size:11px}}
.cd{{font-style:normal;color:#fbbf24;font-size:12px}}
.dia{{margin:0 24px 0 34px;padding:2px 0 8px 22px;border-left:2px solid #2f2722;position:relative}}
.dia h2{{font-size:15px;margin:14px 0 10px;color:#ece6e1;position:relative}}
.dia h2::before{{content:"";position:absolute;left:-30px;top:3px;width:12px;height:12px;border-radius:50%;background:#d97757;box-shadow:0 0 0 4px #d977572e}}
.dia h2 span{{color:#9b8f86;font-weight:400}}
.lista{{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:12px}}
.card{{background:#18141199;border:1px solid #2f2722;border-left:3px solid var(--c);border-radius:12px;padding:12px 16px;transition:.15s;display:flex;flex-direction:column}}
.card:hover{{border-color:#4a3d35;border-left-color:var(--c);transform:translateY(-2px);box-shadow:0 10px 26px #0008}}
.topo{{display:flex;gap:8px;align-items:center;font-size:12px}} .cat{{color:var(--c);font-weight:700}} time{{margin-left:auto;color:#9b8f86}}
.novo{{background:#d97757;color:#111;font-weight:700;font-size:10px;padding:1px 6px;border-radius:4px;text-transform:uppercase}}
.card h3{{font-size:15px;margin:8px 0 2px;line-height:1.35}} .card h3 a{{text-decoration:none}} .card h3 a:hover{{text-decoration:underline}}
.orig{{color:#7d726a;font-size:11px}} .card p{{font-size:13px;color:#cfc5bd;line-height:1.45;margin:8px 0 0}}
details{{margin-top:8px;font-size:13px;color:#cfc5bd}} summary{{cursor:pointer;color:#9b8f86}} ul{{margin:6px 0 0;padding-left:18px;line-height:1.45}}
.acoes{{display:flex;gap:6px;flex-wrap:wrap;margin-top:auto;padding-top:12px}}
.card p.info{{color:#9b8f86;font-size:12px;font-style:italic}} .apagado{{color:#7d726a}} .erro{{color:#f87171;padding:0 24px}}
.termo{{border-bottom:1px dashed #d97757aa;cursor:help}}
#tip{{position:fixed;z-index:9;max-width:300px;background:#221b17f2;border:1px solid #d9775788;border-radius:10px;padding:10px 12px;font-size:12.5px;line-height:1.45;color:#e8dfd8;box-shadow:0 10px 30px #000a;pointer-events:none;display:none}}
#tip b{{display:block;color:#f4a27c;margin-bottom:3px}}
.extra{{padding:10px 24px 0}} .extra>summary{{font-size:15px;color:#ece6e1;padding:10px 0}}
.termos{{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:10px}}
.termo-card{{background:#18141199;border:1px solid #2f2722;border-radius:12px;padding:10px 14px}} .termo-card b{{color:#f4a27c}} .termo-card p{{margin:4px 0 0;color:#cfc5bd;line-height:1.45}}
.todas li{{margin:6px 0;color:#cfc5bd}} .todas b{{color:#ece6e1}}
#vazio{{display:none;color:#9b8f86;padding:30px 24px}}
footer{{padding:30px 24px 40px;color:#6f655e;font-size:12px}}
</style></head><body>
<header><div class="logo"><svg width="20" height="20" viewBox="0 0 64 64"><path d="M32 4C35 24 40 29 60 32C40 35 35 40 32 60C29 40 24 35 4 32C24 29 29 24 32 4Z" fill="#fff7f0"/></svg></div>
<h1>Central do <b>Claude</b><small>atualizado {datetime.now():%d/%m/%Y %H:%M} · {len(itens)} novidades guardadas</small></h1>
<div class="busca"><input id="q" placeholder="Buscar nas novidades (ex.: Haiku, skills...)">
<button id="perguntar" class="btn ia" title="Abre o Claude com a sua pergunta">✨ Perguntar ao Claude</button></div></header>
{aviso_erro}
<div class="stats">
<div class="stat"><b>{semana}</b><span>novidades nos ultimos 7 dias</span></div>
<div class="stat"><b>{versoes}</b><span>versoes do Claude Code em 30 dias</span></div>
<div class="stat"><b>{grandes}</b><span>lancamentos grandes em 90 dias</span></div>
<div class="stat"><b>{prox_txt}</b><span>proxima data da agenda</span></div>
</div>
{hero}
<div class="painel">
<div class="box dica"><h2>\U0001F4A1 Dica do dia</h2><b>{esc(titulo_dica)}</b><p>{esc(texto_dica)}</p></div>
<div class="box agenda"><h2>\U0001F4C5 Agenda (o que vem por ai)</h2><ul>{ag}</ul></div>
</div>
<div class="filtros" id="filtros"><button class="on" data-cat="">Tudo <i>{len(itens)}</i></button>{filtros}</div>
<div id="linha">{"".join(linhas)}</div><p id="vazio">Nada encontrado. Que tal clicar em "Perguntar ao Claude"?</p>
<details class="extra"><summary>\U0001F4D6 Entenda os termos ({len(GLOSSARIO)})</summary><div class="termos">{termos}</div></details>
<details class="extra todas"><summary>\U0001F4DA Todas as dicas ({len(DICAS)})</summary><ul>{todas}</ul></details>
<footer>Passe o mouse nas palavras sublinhadas pra ver o que significam. "Me explica" abre o Claude com a pergunta pronta sobre a novidade.</footer>
<div id="tip"></div>
<script>
let fCat='';
function aplicar(){{
  const t=q.value.toLowerCase(); let n=0;
  document.querySelectorAll('.dia').forEach(s=>{{
    let vis=0;
    s.querySelectorAll('.card').forEach(c=>{{
      const ok=c.dataset.busca.includes(t)&&(!fCat||c.dataset.cat===fCat);
      c.style.display=ok?'':'none'; if(ok)vis++;
    }});
    s.style.display=vis?'':'none'; n+=vis;
  }});
  vazio.style.display=n?'none':'block';
}}
filtros.onclick=e=>{{const b=e.target.closest('button'); if(!b)return;
  filtros.querySelectorAll('button').forEach(x=>x.classList.remove('on')); b.classList.add('on'); fCat=b.dataset.cat; aplicar();}};
q.oninput=aplicar;
perguntar.onclick=()=>{{
  const t=q.value.trim();
  const p=t?'Sobre o Claude (o assistente de IA da Anthropic): '+t+'\\n\\nMe explica em portugues, de um jeito simples, com um exemplo pratico.'
           :'Quais sao as novidades mais recentes do Claude e da Anthropic? Me explica em portugues o que mudou e como aproveitar.';
  open('https://claude.ai/new?q='+encodeURIComponent(p),'_blank');
}};
q.onkeydown=e=>{{if(e.key==='Enter')perguntar.click()}};
document.querySelectorAll('.cd').forEach(el=>{{
  const d=Math.round((new Date(el.dataset.ts*1000).setHours(0,0,0,0)-new Date().setHours(0,0,0,0))/864e5);
  el.textContent=d===0?'hoje':d===1?'amanha':d>1?'em '+d+' dias':'ja passou';
}});
// glossario: sublinha o primeiro aparecimento de cada termo em cada cartao
const G={glossario_js}.map(([n,r,d,f])=>[n,new RegExp(r,f),d]);
function marcar(card){{
  const usados=new Set(), fila=[];
  const w=document.createTreeWalker(card,NodeFilter.SHOW_TEXT,{{acceptNode:x=>x.parentElement.closest('.acoes,.topo,summary,.orig,.info,.termo')?2:1}});
  while(w.nextNode())fila.push(w.currentNode);
  while(fila.length){{
    const no=fila.shift(); let melhor=null;
    for(const [n,re,d] of G){{ if(usados.has(n))continue; const m=no.nodeValue.match(re);
      if(m&&(!melhor||m.index<melhor.m.index))melhor={{n,d,m}}; }}
    if(!melhor)continue;
    usados.add(melhor.n);
    const alvo=no.splitText(melhor.m.index), resto=alvo.splitText(melhor.m[0].length);
    const s=document.createElement('span'); s.className='termo'; s.dataset.n=melhor.n; s.dataset.d=melhor.d;
    alvo.replaceWith(s); s.appendChild(alvo); fila.unshift(no,resto);
  }}
}}
document.querySelectorAll('.card').forEach(marcar);
document.addEventListener('mouseover',e=>{{
  const t=e.target.closest('.termo');
  if(!t){{tip.style.display='none';return}}
  tip.innerHTML='<b></b>'; tip.firstChild.textContent=t.dataset.n; tip.append(t.dataset.d);
  const r=t.getBoundingClientRect(); tip.style.display='block';
  tip.style.left=Math.min(r.left,innerWidth-320)+'px';
  tip.style.top=(r.bottom+8+tip.offsetHeight>innerHeight?r.top-tip.offsetHeight-8:r.bottom+8)+'px';
}});
</script></body></html>""", encoding="utf-8")


# ---------------------------------------------------------------- main
def main():
    itens, agenda, erros = [], [], []
    for nome, fn in (("anuncios e blogs", fonte_feeds), ("Claude Code", fonte_code), ("notas da API", lambda: fonte_api(agenda)),
                     ("notas do app", fonte_app), ("comunidade", fonte_comunidade), ("modelos", lambda: fonte_modelos(agenda) or [])):
        try:
            itens += fn()
        except Exception:
            erros.append(nome)
            log_erro(nome)

    estado = carregar_estado()
    novos, novas_datas, primeira_vez = processar(itens, agenda, estado)
    traduzir_itens(estado)
    try:
        enviar_telegram(estado, novos, novas_datas, primeira_vez)
    except Exception:
        erros.append("telegram")
        log_erro("telegram")
    ESTADO.parent.mkdir(parents=True, exist_ok=True)
    ESTADO.write_text(json.dumps(estado, ensure_ascii=False, indent=0), encoding="utf-8")
    gerar_pagina(estado, erros)
    print(f"{len(estado['itens'])} itens, {len(novos)} novos, {len(estado['agenda'])} datas, erros: {erros or 'nenhum'}")
    if "--abrir" in sys.argv:
        webbrowser.open(PAGINA.as_uri())


if __name__ == "__main__":
    main()
