import torch
import numpy as np
import torch.nn.functional as F
from models.losses import ATLoss
from opt_einsum import contract
from models.GNN import GNN
from torch import nn
from attn_unet import AttentionUNet


def process_long_input(model, input_ids, attention_mask, start_tokens, end_tokens):
    n, c = input_ids.size()
    start_tokens = torch.tensor(start_tokens).to(input_ids)
    end_tokens = torch.tensor(end_tokens).to(input_ids)
    len_start = start_tokens.size(0)
    len_end = end_tokens.size(0)
    if c <= 512:
        output = model(
            input_ids=input_ids,
            attention_mask=attention_mask,
            output_attentions=True,
        )
        sequence_output = output[0]
        attention = output[-1][-1]
    else:
        new_input_ids, new_attention_mask, num_seg = [], [], []
        seq_len = attention_mask.sum(1).cpu().numpy().astype(np.int32).tolist()
        for i, l_i in enumerate(seq_len):
            if l_i <= 512:
                new_input_ids.append(input_ids[i, :512])
                new_attention_mask.append(attention_mask[i, :512])
                num_seg.append(1)
            else:
                input_ids1 = torch.cat([input_ids[i, :512 - len_end], end_tokens], dim=-1)
                input_ids2 = torch.cat([start_tokens, input_ids[i, (l_i - 512 + len_start): l_i]], dim=-1)
                attention_mask1 = attention_mask[i, :512]
                attention_mask2 = attention_mask[i, (l_i - 512): l_i]
                new_input_ids.extend([input_ids1, input_ids2])
                new_attention_mask.extend([attention_mask1, attention_mask2])
                num_seg.append(2)
        input_ids = torch.stack(new_input_ids, dim=0)
        attention_mask = torch.stack(new_attention_mask, dim=0)
        output = model(
            input_ids=input_ids,
            attention_mask=attention_mask,
            output_attentions=True,
        )
        sequence_output = output[0]
        attention = output[-1][-1]
        i = 0
        new_output, new_attention = [], []
        for (n_s, l_i) in zip(num_seg, seq_len):
            if n_s == 1:
                output = F.pad(sequence_output[i], (0, 0, 0, c - 512))
                att = F.pad(attention[i], (0, c - 512, 0, c - 512))
                new_output.append(output)
                new_attention.append(att)
            elif n_s == 2:
                output1 = sequence_output[i][:512 - len_end]
                mask1 = attention_mask[i][:512 - len_end]
                att1 = attention[i][:, :512 - len_end, :512 - len_end]
                output1 = F.pad(output1, (0, 0, 0, c - 512 + len_end))
                mask1 = F.pad(mask1, (0, c - 512 + len_end))
                att1 = F.pad(att1, (0, c - 512 + len_end, 0, c - 512 + len_end))

                output2 = sequence_output[i + 1][len_start:]
                mask2 = attention_mask[i + 1][len_start:]
                att2 = attention[i + 1][:, len_start:, len_start:]
                output2 = F.pad(output2, (0, 0, l_i - 512 + len_start, c - l_i))
                mask2 = F.pad(mask2, (l_i - 512 + len_start, c - l_i))
                att2 = F.pad(att2, [l_i - 512 + len_start, c - l_i, l_i - 512 + len_start, c - l_i])
                mask = mask1 + mask2 + 1e-10
                output = (output1 + output2) / mask.unsqueeze(-1)
                att = (att1 + att2)
                att = att / (att.sum(-1, keepdim=True) + 1e-10)
                new_output.append(output)
                new_attention.append(att)
            i += n_s
        sequence_output = torch.stack(new_output, dim=0)
        attention = torch.stack(new_attention, dim=0)
    return sequence_output, attention


class DocREModel(nn.Module):
    def __init__(self, config, args, model, emb_size=768, block_size=64, num_labels=-1):
        super().__init__()
        self.config = config
        self.bert_model = model
        self.hidden_size = config.hidden_size
        self.loss_fnt = ATLoss()

        self.head_extractor = nn.Linear(1 * config.hidden_size + args.unet_out_dim, emb_size)
        self.tail_extractor = nn.Linear(1 * config.hidden_size + args.unet_out_dim, emb_size)
        self.binary_linear = nn.Linear(emb_size * block_size, config.num_labels)

        self.emb_size = emb_size
        self.block_size = block_size
        self.num_labels = num_labels

        self.bert_drop = nn.Dropout(0.6)
        self.unet_in_dim = args.unet_in_dim
        self.unet_out_dim = args.unet_in_dim
        self.liner = nn.Linear(config.hidden_size, args.unet_in_dim)
        self.min_height = args.max_height
        self.channel_type = args.channel_type
        self.segmentation_net = AttentionUNet(input_channels=args.unet_in_dim,
                                              class_number=args.unet_out_dim,
                                              down_channel=args.down_dim)

        self.offset = 1
        self.gnn = GNN(config.gnn, config.hidden_size + config.gnn.node_type_embedding, args.device)

    def encode(self, input_ids, attention_mask):
        config = self.config
        if config.transformer_type == "bert":
            start_tokens = [config.cls_token_id]
            end_tokens = [config.sep_token_id]
        elif config.transformer_type == "roberta":
            start_tokens = [config.cls_token_id]
            end_tokens = [config.sep_token_id, config.sep_token_id]
        sequence_output, attention = process_long_input(self.bert_model, input_ids, attention_mask, start_tokens,
                                                        end_tokens)
        return sequence_output, attention

    def get_sent_embed(self, sequence_output, batch_sent_pos, num_sent):
        batch_size, _, embed_dim = sequence_output.shape
        sent_embed = torch.zeros((batch_size, num_sent, embed_dim)).to(self.device)
        for batch_id, sent_pos in enumerate(batch_sent_pos):
            for sent_id, pos in enumerate(sent_pos):
                sent_embed[batch_id, sent_id] = sequence_output[batch_id, pos[0] + self.offset]
        return sent_embed

    def get_mention_embed(self, sequence_output, batch_entity_pos, num_mention):
        batch_size, _, embed_dim = sequence_output.shape
        mention_embed = torch.zeros((batch_size, num_mention, embed_dim)).to(self.device)
        for batch_id, entity_pos in enumerate(batch_entity_pos):
            mention_id = 0
            for ent_pos in entity_pos:
                for mention_pos in ent_pos:
                    mention_embed[batch_id, mention_id] = sequence_output[batch_id, mention_pos[0] + self.offset]
                    mention_id += 1
        return mention_embed

    def get_entity_embed(self, sequence_output, batch_entity_pos, num_entity):
        batch_size, _, embed_dim = sequence_output.shape
        entity_embed = torch.zeros((batch_size, num_entity, embed_dim)).to(self.device)
        for batch_id, entity_pos in enumerate(batch_entity_pos):
            for entity_id, ent_pos in enumerate(entity_pos):
                embeds = []
                for mention_pos in ent_pos:
                    embeds.append(sequence_output[batch_id, mention_pos[0] + self.offset])
                entity_embed[batch_id, entity_id] = torch.logsumexp(torch.stack(embeds, dim=0), dim=0)
        return entity_embed

    def get_hrt(self, attention, entity_pos, hts):
        offset = 1 if self.config.transformer_type in ["bert", "roberta"] else 0
        bs, h, _, c = attention.size()

        hss, tss, rss = [], [], []
        entity_es = []
        entity_as = []
        for i in range(len(entity_pos)):
            entity_embs, entity_atts = [], []
            for entity_num, e in enumerate(entity_pos[i]):
                if len(e) > 1:
                    e_emb, e_att = [], []
                    for start, end in e:
                        if start + offset < c:
                            # In case the entity mention is truncated due to limited max seq length.
                            #e_emb.append(sequence_output[i, start + offset])
                            e_att.append(attention[i, :, start + offset])
                    if len(e_emb) > 0:
                        #e_emb = torch.logsumexp(torch.stack(e_emb, dim=0), dim=0)
                        e_att = torch.stack(e_att, dim=0).mean(0)
                    else:
                        #e_emb = torch.zeros(self.config.hidden_size).to(sequence_output)
                        e_att = torch.zeros(h, c).to(attention)
                else:
                    start, end = e[0]
                    if start + offset < c:
                        #e_emb = sequence_output[i, start + offset]
                        e_att = attention[i, :, start + offset]
                    else:
                        #e_emb = torch.zeros(self.config.hidden_size).to(sequence_output)
                        e_att = torch.zeros(h, c).to(attention)
                entity_embs.append(e_emb)
                entity_atts.append(e_att)
            for _ in range(self.min_height - entity_num - 1):
                entity_atts.append(e_att)

            entity_embs = torch.stack(entity_embs, dim=0)  # [n_e, d]
            entity_atts = torch.stack(entity_atts, dim=0)  # [n_e, h, seq_len]

            entity_es.append(entity_embs)
            entity_as.append(entity_atts)
            #ht_i = torch.LongTensor(hts[i]).to(sequence_output.device)
            #hs = torch.index_select(entity_embs, 0, ht_i[:, 0])
            #ts = torch.index_select(entity_embs, 0, ht_i[:, 1])

            #hss.append(hs)
            #tss.append(ts)
        #hss = torch.cat(hss, dim=0)
        #tss = torch.cat(tss, dim=0)
        return entity_as

    def get_virtual_embed(self, sequence_output, batch_virtual_pos, num_virtual):
        batch_size, _, embed_dim = sequence_output.shape
        virtual_embed = torch.zeros((batch_size, num_virtual, embed_dim)).to(self.device)
        for batch_id, virtual_pos in enumerate(batch_virtual_pos):
            for virtual_id, vir_pos in enumerate(virtual_pos):
                if vir_pos[0] == vir_pos[1]:
                    virtual_embed[batch_id][virtual_id] = sequence_output[batch_id][vir_pos[0] + self.offset]
                else:
                    embeds = []
                    for virtual_id2, token_pos in enumerate(virtual_pos):
                        if vir_pos[0] <= token_pos[0] < vir_pos[1]:
                            embeds.append(sequence_output[batch_id][token_pos[0] + self.offset])

                    virtual_embed[batch_id][virtual_id] = torch.logsumexp(torch.stack(embeds, dim=0), dim=0)
        return virtual_embed

    def get_ht(self, rel_enco, hts):
        htss = []
        for i in range(len(hts)):
            ht_index = hts[i]
            for (h_index, t_index) in ht_index:
                htss.append(rel_enco[i, h_index, t_index])
        htss = torch.stack(htss, dim=0)
        return htss

    def get_rss(self, sequence_output, attention, entity_pos, hts):
        offset = 1 if self.bert_config.transformer_type in ["bert", "roberta"] else 0
        n, h, _, c = attention.size()
        hss, tss, rss = [], [], []
        for i in range(len(entity_pos)):
            entity_atts = []
            for e in entity_pos[i]:
                if len(e) > 1:
                    e_att = []
                    for start, end in e:
                        if start + offset < c:
                            e_att.append(attention[i, :, start + offset])
                    if len(e_att) > 0:
                        e_att = torch.stack(e_att, dim=0).mean(0)
                    else:
                        e_att = torch.zeros(h, c).to(attention)
                else:
                    start, end = e[0]
                    if start + offset < c:
                        e_att = attention[i, :, start + offset]
                    else:
                        e_att = torch.zeros(h, c).to(attention)
                entity_atts.append(e_att)
            entity_atts = torch.stack(entity_atts, dim=0)  # [n_e, h, seq_len]

            ht_i = torch.LongTensor(hts[i]).to(sequence_output.device)

            h_att = torch.index_select(entity_atts, 0, ht_i[:, 0])
            t_att = torch.index_select(entity_atts, 0, ht_i[:, 1])
            ht_att = (h_att * t_att).mean(1)
            ht_att = ht_att / (ht_att.sum(1, keepdim=True) + 1e-5)
            rs = contract("ld,rl->rd", sequence_output[i], ht_att)
            rss.append(rs)
        rss = torch.cat(rss, dim=0)
        # print(rss.shape)
        return rss

    def forward(self, input_ids, attention_mask,
                entity_pos, sent_pos, virtual_pos,
                graph, num_mention, num_entity, num_sent, num_virtual,
                labels=None, hts=None):
        sequence_output, attention = self.encode(input_ids, attention_mask)
        mention_embed = self.get_mention_embed(sequence_output, entity_pos, num_mention)
        entity_embed = self.get_entity_embed(sequence_output, entity_pos, num_entity)
        sent_embed = self.get_sent_embed(sequence_output, sent_pos, num_sent)
        virtual_embed = self.get_virtual_embed(sequence_output, virtual_pos, num_virtual)
        entity_hidden_state = self.gnn([mention_embed, entity_embed, sent_embed, virtual_embed, graph])
        s_embed, t_embed = self.get_pair_entity_embed(entity_hidden_state, hts)


        if self.channel_type == 'context-based':
            feature_map = self.get_rss(sequence_output, attention, entity_pos, hts)
            ##print('feature_map:', feature_map.shape)
            attn_input = self.liner(feature_map).permute(0, 3, 1, 2).contiguous()
        else:
            raise Exception("channel_type must be specify correctly")

        attn_map = self.segmentation_net(attn_input)
        local_context = self.get_ht(attn_map, hts)

        s_embed = torch.tanh(self.head_extractor(torch.cat([s_embed, local_context], dim=1)))
        t_embed = torch.tanh(self.tail_extractor(torch.cat([t_embed, local_context], dim=1)))

        b1 = s_embed.view(-1, self.emb_size // self.block_size, self.block_size)
        b2 = t_embed.view(-1, self.emb_size // self.block_size, self.block_size)
        bl = (b1.unsqueeze(3) * b2.unsqueeze(2)).view(-1, self.emb_size * self.block_size)
        logits = self.binary_linear(bl)

        output = (self.loss_fnt.get_label(logits, num_labels=self.num_labels),)
        if labels is not None:
            labels = [torch.tensor(label) for label in labels]
            labels = torch.cat(labels, dim=0).to(logits)
            loss = self.loss_fnt(logits.float(), labels.float())
            output = (loss.to(sequence_output),) + output
        return output
