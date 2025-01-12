import torch
import torch.nn as nn
import torch.nn.functional as F
from dialogue.models.base import BaseDeepModel
from dialogue.toolbox.beam import BeamSeqs
from dialogue.toolbox.layers import SortedGRU, Attention
from dialogue.toolbox.utils import batch_unpadding
from pytorch_pretrained_bert.modeling import BertModel

class ASEREncoderDecoder(BaseDeepModel):
    def __init__(self, loss_fn, opt):
        super(ASEREncoderDecoder, self).__init__()
        rnn_hidden_size = opt.model.rnn_hidden_size

        self.encoder_embedding = nn.Embedding(opt.model.word_vocab_size, opt.model.word_embed_size)
        self.decoder_embedding = self.encoder_embedding
        self.event_id_embedding = nn.Embedding(opt.model.aser_vocab_size, rnn_hidden_size // 4)
        self.event_embedding = nn.Embedding(opt.model.aser_event_vocab_size, rnn_hidden_size // 4)
        self.relation_embedding = nn.Embedding(opt.model.aser_relation_vocab_size, rnn_hidden_size // 4)
        # self.encoder = SortedGRU(input_size=opt.model.word_embed_size,
        #                          hidden_size=opt.model.rnn_hidden_size // 2,
        #                          num_layers=opt.model.n_layers,
        #                          batch_first=True,
        #                          bidirectional=True)
        self.decoder = SortedGRU(input_size=opt.model.word_embed_size,
                                 hidden_size=opt.model.rnn_hidden_size,
                                 num_layers=opt.model.n_layers,
                                 batch_first=True,
                                 bidirectional=False)
        self.attn = Attention(input_size=opt.model.rnn_hidden_size, method=opt.model.attn_score_method)
        self.dropout = nn.Dropout(opt.model.dropout)
        self.concat = nn.Linear(
            rnn_hidden_size * 2 + rnn_hidden_size * opt.model.use_word_attn, rnn_hidden_size)
        #TODO
        # self.bert2hiddensize = nn.Linear(768, opt.model.rnn_hidden_size)
        # self.bert2hiddensize2 = nn.Linear(768, opt.model.rnn_hidden_size)

        self.fc = nn.Linear(rnn_hidden_size, opt.model.word_vocab_size)

        self.loss_fn = loss_fn
        self.rnn_hidden_size = opt.model.rnn_hidden_size
        self.n_layers = opt.model.n_layers
        self.use_word_attn = opt.model.use_word_attn
        self.use_cuda = opt.meta.use_cuda
        self.bertmodel = BertModel.from_pretrained('bert-base-uncased')
        for p in self.bertmodel.parameters():
            p.requires_grad = False


    def encode(self, encoder_inputs, encoder_lens, bert_post_ids, bert_post_masks):
        # encoder_embeds = self.encoder_embedding(encoder_inputs)
        # encoder_outputs, last_hidden = self.encoder(
        #     encoder_embeds, encoder_lens)
        encoder_outputs2, last_hidden2 = self.bertmodel(bert_post_ids, token_type_ids=None, attention_mask=bert_post_masks,output_all_encoded_layers=False)
        # TODO
        # b, s, _ = encoder_outputs2.size()
        # encoder_outputs2 = self.bert2hiddensize(encoder_outputs2.reshape(b*s, 768))
        # encoder_outputs2 = encoder_outputs2.reshape(b, s, 512)

        # last_hidden2 = self.bert2hiddensize2(last_hidden2)
        last_hidden2 = last_hidden2.unsqueeze(0)
        last_hidden2 = last_hidden2.repeat(self.n_layers, 1, 1)

        return encoder_outputs2, last_hidden2

    def encode_events(self, event_id_inputs, event_triple_inputs):
        event_id_embs = self.event_id_embedding(event_id_inputs)
        event1_embs = self.event_embedding(event_triple_inputs[:, :, 0])
        rel_embs = self.relation_embedding(event_triple_inputs[:, :, 1])
        event2_embs = self.event_embedding(event_triple_inputs[:, :, 2])
        event_embs = torch.cat(
            [event_id_embs, event1_embs, rel_embs, event2_embs], dim=-1)
        return event_embs

    def decode(self, encoder_outputs, encoder_lens,
               event_embs, event_lens,
               last_hidden, decoder_inputs,
               bert_responses_ids=None, bert_responses_masks=None,
               decoder_lens=None):
        decoder_embeds = self.decoder_embedding(decoder_inputs)
        # decoder_embeds2 = self.bertmodel.embeddings(bert_responses_ids, bert_responses_masks)
        decoder_outputs, last_hidden = self.decoder(
            decoder_embeds, decoder_lens, last_hidden)
        event_context, _ = self.attn(decoder_outputs, event_embs,
                                q_lens=decoder_lens, k_lens=event_lens)
        if self.use_word_attn:
            word_contexts, _ = self.attn(decoder_outputs, encoder_outputs,
                                    q_lens=decoder_lens, k_lens=encoder_lens)
            outlayer_inputs = torch.cat([decoder_outputs, word_contexts, event_context], dim=2)
        else:
            outlayer_inputs = torch.cat([decoder_outputs, event_context], dim=2)
        outlayer_outputs = torch.tanh(self.concat(outlayer_inputs))
        decoder_outputs = self.fc(outlayer_outputs)
        return decoder_outputs, last_hidden

    def forward(self, encoder_inputs, encoder_lens, decoder_inputs, decoder_lens,
                event_id_inputs, event_triple_inputs, event_lens,
                bert_post_ids, bert_responses_ids, bert_post_masks, bert_responses_masks):
        encoder_outputs, encoder_last_hidden = self.encode(encoder_inputs, encoder_lens,
            bert_post_ids, bert_post_masks)

        # encoder_last_hidden = self._fix_hidden(encoder_last_hidden)

        event_embs = self.encode_events(event_id_inputs, event_triple_inputs)

        decoder_outputs, _ = self.decode(
            encoder_outputs, encoder_lens,
            event_embs, event_lens,
            encoder_last_hidden, decoder_inputs, 
            bert_responses_ids, bert_responses_masks, decoder_lens)
        outputs = F.log_softmax(decoder_outputs, dim=2)
        return outputs

    def generate(self, encoder_inputs, encoder_lens,decoder_start_input,
                 event_id_inputs, event_triple_inputs, event_lens, bert_post_ids, bert_post_masks, 
                 max_len, beam_size=1, eos_val=None):
        encoder_outputs, encoder_last_hidden = self.encode(encoder_inputs, encoder_lens, bert_post_ids, bert_post_masks)  # TODO
        # encoder_last_hidden = self._fix_hidden(encoder_last_hidden)

        event_embs = self.encode_events(event_id_inputs, event_triple_inputs)

        beamseqs = BeamSeqs(beam_size=beam_size)
        beamseqs.init_seqs(seqs=decoder_start_input[0], init_state=encoder_last_hidden)
        done = False
        for i in range(max_len):
            for j, (seqs, _, last_token, last_hidden) in enumerate(beamseqs.current_seqs):
                if beamseqs.check_and_add_to_terminal_seqs(j, eos_val):
                    if len(beamseqs.terminal_seqs) >= beam_size:
                        done = True
                        break
                    continue
                out, last_hidden = self.decode(encoder_outputs, encoder_lens,
                                               event_embs, event_lens,
                                               last_hidden, last_token.unsqueeze(0))
                _output = F.log_softmax(out.squeeze(0), dim=1).squeeze(0)
                scores, tokens = _output.topk(beam_size * 2)
                for k in range(beam_size * 2):
                    score, token = scores.data[k], tokens[k]
                    token = token.unsqueeze(0)
                    beamseqs.add_token_to_seq(j, token, score, last_hidden)
            if done:
                break
            beamseqs.update_current_seqs()
        final_seqs = beamseqs.return_final_seqs()
        return final_seqs[0].unsqueeze(0)

    @staticmethod
    def _fix_hidden(hidden):
        # The encoder hidden is  (layers*directions) x batch x dim.
        # We need to convert it to layers x batch x (directions*dim).
        hidden = torch.cat([hidden[0:hidden.size(0):2],
                            hidden[1:hidden.size(0):2]], 2)
        return hidden

    # def flatten_parameters(self):
    #     self.encoder.flatten_parameters()
    #     self.decoder.flatten_parameters()

    def run_batch(self, batch):
        # print(batch.bert_post_ids)
        enc_inps, enc_lens = batch.enc_inps
        dec_inps, dec_lens = batch.dec_inps
        dec_tgts, _ = batch.dec_tgts

        dec_probs = self.forward(
            encoder_inputs=enc_inps, encoder_lens=enc_lens,
            decoder_inputs=dec_inps, decoder_lens=dec_lens,
            event_id_inputs=batch.aser_id_inps,
            event_triple_inputs=batch.aser_triple_inps,
            event_lens=batch.aser_lens,
            bert_post_ids=batch.bert_post_ids,
            bert_responses_ids=batch.bert_responses_ids,
            bert_post_masks=batch.bert_post_masks,
            bert_responses_masks=batch.bert_responses_masks
            )

        decoder_probs_pack = dec_probs.view(-1, dec_probs.size(2))
        decoder_targets_pack = dec_tgts.view(-1)
        loss = self.loss_fn(decoder_probs_pack, decoder_targets_pack)
        decoder_probs_pack = batch_unpadding(dec_probs, dec_lens)
        decoder_targets_pack = batch_unpadding(dec_tgts, dec_lens)
        _, pred = decoder_probs_pack.max(1)
        num_correct = pred.eq(decoder_targets_pack).sum().item()
        num_words = pred.size(0)
        result_dict = {
            "loss": loss,
            "num_correct": num_correct,
            "num_words": num_words,
        }
        return result_dict

    def predict_batch(self, batch, max_len=20, beam_size=4, eos_val=0):
        enc_inps, enc_lens = batch.enc_inps
        dec_start_inps = batch.dec_start_inps
        preds = self.generate(encoder_inputs=enc_inps, encoder_lens=enc_lens,
                              decoder_start_input=dec_start_inps,
                              event_id_inputs=batch.aser_id_inps,
                              event_triple_inputs=batch.aser_triple_inps,
                              event_lens=batch.aser_lens,
                              bert_post_ids=batch.bert_post_ids,
                              bert_post_masks=batch.bert_post_masks,
                              max_len=max_len, beam_size=beam_size, eos_val=eos_val).squeeze(2)
        preds = preds.data.cpu().numpy()
        return preds