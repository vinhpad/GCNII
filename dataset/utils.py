import json
from tqdm import tqdm
from typing import List, Any

cdr_rel2id = {'1:NR:2': 0, '1:CID:2': 1}

def chunks(l, n):
    res = []
    for i in range(0, len(l), n):
        assert len(l[i:i + n]) == n
        res += [l[i:i + n]]
    return res

def read_cdr(file_in, tokenizer, max_seq_length=1024) -> List[Any]:
    pmids = set()
    features = []
    maxlen = 0
    with open(file_in, 'r') as infile:
        lines = infile.readlines()
        for i_l, line in enumerate(tqdm(lines)):
            line = line.rstrip().split('\t')
            pmid = line[0]

            if pmid not in pmids:
                pmids.add(pmid)
                text = line[1]
                prs = chunks(line[2:], 17)

                ent2idx = {}
                train_triples = {}

                entity_pos = set()
                for p in prs:
                    # print(p)
                    es = list(map(int, p[8].split(':')))
                    ed = list(map(int, p[9].split(':')))
                    tpy = p[7]
                    # print(es)
                    # print(ed)
                    for start, end in zip(es, ed):
                        entity_pos.add((start, end, tpy))
                        # print((start, end, tpy))

                    es = list(map(int, p[14].split(':')))
                    ed = list(map(int, p[15].split(':')))
                    tpy = p[13]
                    # print(es)
                    # print(ed)
                    for start, end in zip(es, ed):
                        entity_pos.add((start, end, tpy))
                        # print((start, end, tpy))
                    # print('\n\n')

                sents = [t.split(' ') for t in text.split('|')]
                new_sents = []
                sent_map = {}
                sent_pos = []
                i_t = 0
                for sent in sents:
                    start_sent = len(new_sents)
                    new_sents.append('[SENT]')
                    for token in sent:
                        # print(i_t, token)
                        tokens_wordpiece = tokenizer.tokenize(token)
                        for start, end, tpy in list(entity_pos):
                            if i_t == start:
                                tokens_wordpiece = ["[ENTITY]"] + tokens_wordpiece
                            if i_t + 1 == end:
                                tokens_wordpiece = tokens_wordpiece + ["[/ENTITY]"]
                        # print(new_sents)
                        sent_map[i_t] = len(new_sents)
                        new_sents.extend(tokens_wordpiece)
                        i_t += 1
                    end_sent = len(new_sents)
                    sent_pos.append((start_sent, end_sent))
                    sent_map[i_t] = len(new_sents)
                    new_sents.append('[/SENT]')
                sents = new_sents
                entity_pos = []
                for p in prs:
                    if p[0] == "not_include":
                        continue
                    if p[1] == "L2R":
                        h_id, t_id = p[5], p[11]
                        h_start, t_start = p[8], p[14]
                        h_end, t_end = p[9], p[15]
                    else:
                        t_id, h_id = p[5], p[11]
                        t_start, h_start = p[8], p[14]
                        t_end, h_end = p[9], p[15]
                    # print(h_id, t_id, h_start, t_start, h_end, t_end)
                    h_start = map(int, h_start.split(':'))
                    h_end = map(int, h_end.split(':'))
                    t_start = map(int, t_start.split(':'))
                    t_end = map(int, t_end.split(':'))
                    h_start = [sent_map[idx] for idx in h_start]
                    h_end = [sent_map[idx] for idx in h_end]
                    t_start = [sent_map[idx] for idx in t_start]
                    t_end = [sent_map[idx] for idx in t_end]
                    if h_id not in ent2idx:
                        ent2idx[h_id] = len(ent2idx)
                        entity_pos.append(list(zip(h_start, h_end)))
                    if t_id not in ent2idx:
                        ent2idx[t_id] = len(ent2idx)
                        entity_pos.append(list(zip(t_start, t_end)))
                    h_id, t_id = ent2idx[h_id], ent2idx[t_id]

                    r = cdr_rel2id[p[0]]
                    if (h_id, t_id) not in train_triples:
                        train_triples[(h_id, t_id)] = [{'relation': r}]
                    else:
                        train_triples[(h_id, t_id)].append({'relation': r})
                # print(ent2idx)
                relations, hts = [], []
                for h, t in train_triples.keys():
                    relation = [0] * len(cdr_rel2id)
                    for mention in train_triples[h, t]:
                        relation[mention["relation"]] = 1
                    # print(h, t, rel)
                    relations.append(relation)
                    hts.append([h, t])

            maxlen = max(maxlen, len(sents))
            sents = sents[:max_seq_length - 2]
            input_ids = tokenizer.convert_tokens_to_ids(sents)
            input_ids = tokenizer.build_inputs_with_special_tokens(input_ids)
            if len(hts) > 0:
                feature = {'input_ids': input_ids,
                           'entity_pos': entity_pos,
                           'labels': relations,
                           'hts': hts,
                           'title': pmid,
                           'sent_pos': sent_pos
                           }
                features.append(feature)
    print("Number of documents: {}.".format(len(features)))
    print("Max document length: {}.".format(maxlen))
    return features
