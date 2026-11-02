"""Optional offline NMT worker; imported dependencies live in a separate venv.

Invoked by paper_translation inside a network-disabled Linux sandbox. Never
install packages or download weights here. The input contains abstracts only.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys


def sentence_tokens(text, tokenizer):
    """Bound each decoding unit without silently dropping long input tails."""
    for sentence in re.split(r"(?<=[.!?。！？])\s+|\n\s*\n", text):
        tokens = tokenizer.encode(sentence.strip(), out_type=str)
        for start in range(0, len(tokens), 256):
            yield tokens[start:start + 256]


def translate_summary(text, tokenizer, model):
    tokens = list(sentence_tokens(text, tokenizer))
    if not tokens:
        return ""
    results = model.translate_batch(tokens, beam_size=4, length_penalty=0.2,
                                    replace_unknowns=True, max_batch_size=16,
                                    max_input_length=0, max_decoding_length=1024)
    pieces = []
    for result in results:
        output = result.hypotheses[0]
        if len(output) >= 1024:
            raise ValueError("translation exceeded decoding limit")
        # Argos' exported OPUS vocabulary can retain the SentencePiece marker.
        pieces.append(tokenizer.decode(output).replace("▁", " ").strip())
    return " ".join(pieces)


def main():
    import ctranslate2
    import sentencepiece

    root = Path("/translator/package/translate-en_zh-1_9")
    tokenizer = sentencepiece.SentencePieceProcessor(model_file=str(root / "sentencepiece.model"))
    model = ctranslate2.Translator(str(root / "model"), device="cpu", compute_type="int8",
                                   intra_threads=2, inter_threads=1)
    rows = json.loads(sys.stdin.buffer.read(8 * 1024 * 1024 + 1))
    if not isinstance(rows, list) or len(rows) > 100:
        raise ValueError("invalid translation batch")
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {"id", "summary"}
                or not isinstance(row["id"], str) or len(row["id"]) > 64
                or not isinstance(row["summary"], str) or not 0 < len(row["summary"]) <= 20000):
            raise ValueError("invalid abstract")
        translated = translate_summary(row["summary"], tokenizer, model)
        if not translated or len(translated) > 40000:
            raise ValueError("invalid translation")
        print(json.dumps({"id": row["id"], "summary_zh": translated}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
