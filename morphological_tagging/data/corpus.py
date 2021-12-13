from torch.utils.data import Dataset, DataLoader
import os
import csv
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import List, NamedTuple
from collections import Counter, defaultdict

import torch
import torch.nn as nn
from torch.nn.utils.rnn import pad_sequence
import torch.nn.functional as F
import torchtext
from torchtext.vocab import Vocab, vocab, build_vocab_from_iterator, Vectors
import pandas as pd

from morphological_tagging.data.lemma_script import LemmaScriptGenerator

class FastText(Vectors):

    url_base = "https://dl.fbaipublicfiles.com/fasttext/vectors-crawl/cc.{}.300.vec.gz"

    def __init__(self, language="en", **kwargs):
        url = self.url_base.format(language)
        name = os.path.basename(url)
        super(FastText, self).__init__(name, url=url, **kwargs)


@dataclass
class Tree:
    """A class for holding a single tree.
    """

    raw: List = field(default_factory=lambda: [])
    tokens: List = field(default_factory=lambda: [])
    lemmas: List = field(default_factory=lambda: [])
    morph_tags: List = field(default_factory=lambda: [])

    def add(self, branch: List):
        _, word_form, lemma, _, _, morph_tags, _, _, _, _ = branch

        self.raw.append((word_form, lemma, morph_tags))
        self.tokens.append(word_form)
        self.lemmas.append(lemma)
        self.morph_tags.append(morph_tags.rsplit(";"))

    def __len__(self):
        return len(self.raw)

    def __getitem__(self, i: int):
        return self.raw[i]

    def __str__(self):
        return f"Tree({self.raw})"

    def __repr__(self):
        return self.__str__()


@dataclass
class Document:
    """A class for holding a single text document.
    """

    sent_id: str = None
    text: str = None
    tree: List = field(default_factory=lambda: Tree())

    def __str__(self):
        return f"Doc(sent_id={self.sent_id})"

    def __repr__(self):
        return self.__str__()

    def __len__(self):
        return len(self.tree.tokens)

    @property
    def tokens(self):
        return self.tree.tokens

    @property
    def lemmas(self):
        return self.tree.lemmas

    @property
    def morph_tags(self):
        return self.tree.morph_tags

    def set_tensors(self, chars_tensor, tokens_tensor, morph_tags_tensor):

        self.chars_tensor = chars_tensor

        self.tokens_tensor = tokens_tensor

        self.morph_tags_tensor = morph_tags_tensor

    def set_lemma_tags(self, tags_tensor: torch.LongTensor):

        self.lemma_tags_tensor = tags_tensor

    def set_word_embeddings(
        self, word_emb: torch.Tensor, word_vec_type: str = "Undefined"
    ):

        self.word_emb_type = word_vec_type
        self.word_emb = word_emb

    def set_context_embeddings(
        self, context_emb: torch.Tensor, model_name: str = "Undefined"
    ):

        self.context_emb = context_emb
        self.model_name = model_name


@dataclass
class DocumentCorpus(Dataset):
    """A class for reading, holding and processing many documents.
    """

    docs: List = field(default_factory=lambda: [])
    unk_token: str = "<UNK>"
    pad_token: str = "<PAD>"

    def __len__(self):
        return len(self.docs)

    def __getitem__(self, i: int):
        return self.docs[i]

    def __str__(self):
        return f"DocumentCorpus(len={len(self.docs)})"

    def __repr__(self):
        return self.__str__()

    def clear_docs(self):

        self.docs = []

    def _get_vocabs(self):

        self.token_vocab = build_vocab_from_iterator(
            [[t for t in d.tokens] for d in self.docs],
            specials=[self.unk_token, self.pad_token],
            special_first=True
        )
        self.token_vocab.set_default_index(self.token_vocab[self.unk_token])

        self.char_vocab = build_vocab_from_iterator(
            [[c for c in t] for d in self.docs for t in d.tokens],
            specials=[self.unk_token, self.pad_token],
            special_first=True
        )
        self.char_vocab.set_default_index(self.token_vocab[self.unk_token])

        self.morph_tag_vocab = {
            k: v for v, k in enumerate(sorted(
                {tag for d in self.docs for tagset in d.morph_tags for tag in tagset}
                ))
            }


    def _move_to_pt(self):

        for d in self.docs:
            chars_tensor = [
                    torch.tensor(self.char_vocab.lookup_indices([c for c in t]),
                                 dtype=torch.long
                                 #TODO (ivo): add device support
                                )
                    for t in d.tokens
                ]

            tokens_tensor = torch.tensor(
                self.token_vocab.lookup_indices([t for t in d.tokens]),
                dtype=torch.long
                #TODO (ivo): add device support
            )

            morph_tags_tensor = torch.cat([
                F.one_hot(
                    torch.tensor(
                        [self.morph_tag_vocab.get(tag, '_') for tag in tagset],
                        dtype=torch.long
                        #TODO (ivo): add device support
                        ),
                    len(self.morph_tag_vocab)
                )[:,:-1]
            for tagset in d.morph_tags])

            d.set_tensors(
                chars_tensor,
                tokens_tensor,
                morph_tags_tensor
            )

    def parse_tree_file(self, fp: str):
        """Parse a single document with CONLL-U trees into a list of Documents.
        Will append to documents, not overwrite.

        Args:
            fp (str): filepath to the document.

        """
        with open(fp, newline="\n", encoding="utf8") as csvfile:
            conllu_data = csv.reader(csvfile, delimiter="\t", quotechar="\u2400")

            cur_doc = Document()
            for i, row in enumerate(conllu_data):

                # New sentence
                if len(row) == 0:

                    self.docs.append(cur_doc)
                    cur_doc = Document()

                # Get sentence ID
                elif "# sent_id = " in row[0]:
                    sent_id = row[0][12:]

                    cur_doc.sent_id = sent_id

                # Get sentence in plain language (non-tokenized)
                elif "# text = " in row[0]:
                    full_text = row[0][9:]

                    cur_doc.text = full_text

                # Get tree information
                # CONLL-U format
                elif len(row) > 1:
                    if "." in row[0]:
                        continue
                    cur_doc.tree.add(row)

            self.docs.append(cur_doc)

        self._get_vocabs()
        self._move_to_pt()

    def add_word_embs(
        self, vecs: Vectors = FastText, lower_case_backup: bool = False, **kwargs
    ):
        """Add pre-trained word embeddings to a collection of documents.

        Args:
            docs (List[Document]): [description]
            vecs (Vectors, optional): [description]. Defaults to FastText.
            lower_case_backup (bool, optional): [description]. Defaults to False.

        """

        embeds = vecs(**kwargs)

        for d in self.docs:
            d.set_word_embeddings(
                embeds.get_vecs_by_tokens(d.tokens, lower_case_backup), vecs.__name__
            )

    def add_context_embs(self, model, tokenizer):
        """Generate contextual embedding from document text.

        Args:
            d (Document): [description]
            tokenizer (Huggingface Tokenizer):
            model (Huggingface Transformer):

        """

        for d in self.docs:
            # Tokenize the whole text
            s_tokenized = tokenizer(d.text, return_offsets_mapping=True)

            # Find the token spans within the text
            end, spans = 0, []
            for t in d.tokens:
                match = re.search(re.escape(t), d.text[end:])

                spans.append((match.span()[0] + end, match.span()[1] + end))

                end += match.span()[-1]

            # Find correspondence of tokenized string and dataset tokens
            index, correspondence = 0, defaultdict(list)
            for i, (_, tokenized_end) in enumerate(s_tokenized["offset_mapping"][1:-1]):
                # Iterate through the offset_mapping of the tokenizer
                # add the tokenized token to the mapping for the original token
                correspondence[index].append(i)

                # Increment the index if tokenized token span is exhausted
                if tokenized_end == spans[index][-1]:
                    index += 1

            # Convert from defaultdict to regular dict
            correspondence = dict(correspondence)

            # Get contextualized embeddings
            contextual_embeddings = model(
                **tokenizer(d.text, return_tensors="pt"), output_hidden_states=True
            )

            contextual_embeddings = torch.stack(
                contextual_embeddings["hidden_states"][-4:]
            )
            contextual_embeddings = torch.mean(contextual_embeddings, dim=0).squeeze()

            contextual_embeddings_corresponded = torch.stack(
                [
                    torch.mean(contextual_embeddings[correspondence[k]], dim=0)
                    for k in correspondence.keys()
                ]
            )

            d.set_context_embeddings(
                contextual_embeddings_corresponded, type(model).__name__
            )

    def set_lemma_tags(self):

        # Iterate over all documents once to get stats on the lemma scripts
        self.script_counter, self.script_examples = Counter(), defaultdict(set)
        docs_scripts = []
        for i, d in enumerate(self.docs):

            doc_scripts = []
            for wf, lm in zip(d.tokens, d.lemmas):

                lemma_script = LemmaScriptGenerator(wf, lm).get_lemma_script()
                self.script_counter[lemma_script] += 1

                doc_scripts.append(lemma_script)

                if len(self.script_examples[lemma_script]) < 3:
                    self.script_examples[lemma_script].add(f"{wf}\u2192{lm}")

            docs_scripts.append(doc_scripts)

        # Generate script to class conversion
        self.script_to_id = {
            k: i
            for i, (k, _) in enumerate(sorted(self.script_counter.items(), key=lambda x: x[1], reverse=True))
        }

        self.id_to_script = list(self.script_to_id.keys())

        # Add the scripts as classes to the individual documents
        for i, doc_scripts in enumerate(docs_scripts):

            self.docs[i].set_lemma_tags(
                torch.tensor(
                    [self.script_to_id[script] for script in doc_scripts]
                    , dtype = torch.long #TODO (ivo) add device support
                    )
                )

    def lemma_tags_overview(self, n: int = 11) -> pd.DataFrame:
        """Get the most common lemma scripts and some examples.

        Args:
            n (int, optional): number of scripts to show. Defaults to 11.
        """

        most_common_rules = [[script, count] for script, count in self.script_counter.most_common(n)]

        for entry in most_common_rules:
            entry.append(self.script_examples[entry[0]])

        df = pd.DataFrame(most_common_rules, columns=["Rule", "Count", "Examples"])

        return df