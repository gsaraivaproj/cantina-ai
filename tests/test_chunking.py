from cantina_ai.engine.chunking import chunk_note, estimate_tokens, note_title

NOTE = """---
tipo: projeto
---
# Slack Monitor

Intro do projeto, com texto suficiente para não ser considerada uma seção minúscula
demais: ela precisa passar de uns trinta tokens estimados para ficar sozinha.

## Operação diária

```bash
# status
launchctl list | grep slack
```

Ver os logs quando um tick falhar, e nunca usar o sucesso do cron como prova de que funcionou.

## Repositório
- Remote: x

## Histórico
Texto do histórico, longo o bastante para ficar sozinho como um trecho próprio da nota.
"""


def test_title_is_the_first_h1_outside_the_frontmatter() -> None:
    assert note_title(NOTE, "a/b.md") == "Slack Monitor"
    assert note_title("sem título", "a/Minha Nota.md") == "Minha Nota"


def test_sections_become_chunks_with_a_context_header() -> None:
    chunks = chunk_note(NOTE, "Slack Monitor")

    assert [c.heading_path for c in chunks] == [
        (),
        ("Operação diária",),
        ("Repositório + Histórico",),
    ]
    assert chunks[1].embedded_text.startswith("Slack Monitor > Operação diária\n\n")
    assert "tipo: projeto" not in chunks[0].content  # frontmatter stripped


def test_a_heading_inside_a_code_block_is_not_a_section() -> None:
    chunks = chunk_note(NOTE, "Slack Monitor")

    assert "# status" in chunks[1].content
    assert all("status" not in c.heading_path for c in chunks)


def test_a_tiny_section_joins_the_next_keeping_its_heading() -> None:
    [*_, last] = chunk_note(NOTE, "Slack Monitor")

    assert last.content.startswith("**Repositório**\n- Remote: x")
    assert "Texto do histórico" in last.content


def test_a_long_section_is_split_by_paragraph_with_overlap() -> None:
    paragraphs = [f"Parágrafo {i}. " + "palavra " * 10 for i in range(8)]
    text = "# T\n\n## Longa\n\n" + "\n\n".join(paragraphs)

    chunks = chunk_note(text, "T", max_tokens=100)

    assert len(chunks) > 1
    assert all(c.heading_path == ("Longa",) for c in chunks)
    # The last paragraph of a piece starts the next one.
    first_last_paragraph = chunks[0].content.split("\n\n")[-1]
    assert chunks[1].content.startswith(first_last_paragraph)


def test_one_huge_block_is_cut_between_lines() -> None:
    lines = "\n".join(f"- [x] item {i} " + "texto " * 30 for i in range(40))
    chunks = chunk_note(f"# T\n\n## Lista\n\n{lines}", "T", max_tokens=200)

    assert len(chunks) > 1
    assert all(c.token_estimate < 260 for c in chunks)


def test_estimate_is_about_four_characters_per_token() -> None:
    assert estimate_tokens("a" * 400) == 100


def test_a_navigation_footer_is_dropped() -> None:
    text = "# T\n\n## Links\n\n- [[Outra nota]]\n- Site: https://x\n\n---\n[[_index|← voltar]]\n"

    [chunk] = chunk_note(text, "T")

    assert chunk.content == "- [[Outra nota]]\n- Site: https://x"  # bullets with links stay


def test_a_merged_chunk_is_labelled_with_every_heading() -> None:
    text = "# T\n\n## Stack\n- Elixir\n\n## Repositório\n" + "texto do repositório " * 10

    [chunk] = chunk_note(text, "T")

    assert chunk.heading_path == ("Stack + Repositório",)
    assert chunk.embedded_text.startswith("T > Stack + Repositório\n\n**Stack**\n- Elixir")
