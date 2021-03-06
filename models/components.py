from typing import Sequence

from torch.autograd import Variable as Var
from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence
import torch
import torch.nn as nn


class ContextWindow(nn.Module):
    def __init__(self, window: int) -> None:
        super(ContextWindow, self).__init__()
        self.window = window

    def forward(self, inputs: Var) -> Var:
        assert inputs.dim() == 3

        if self.window == 0:
            return inputs

        batch_size, seq_length = inputs.size(0), inputs.size(1)

        # shape: (batch_size, seq_length + window, size)
        inputs = self._pad(inputs, pad_first=True)
        # shape: (batch_size, seq_length + 2*window, size)
        inputs = self._pad(inputs, pad_first=False)

        temps = []
        for i in range(seq_length):
            lo, hi = i, i + 2 * self.window + 1
            # shape: (batch_size, (2*window + 1) * size)
            temp = inputs[:, lo:hi, :].contiguous().view(batch_size, -1)
            temps.append(temp)
        # shape: (batch_size, seq_length, (2*window + 1) * size)
        outputs = torch.stack(temps, dim=1)

        return outputs

    def __repr__(self) -> str:
        return f'{self.__class__.__name__}(window={self.window})'

    def _pad(self, x: Var, pad_first: bool = True) -> Var:
        assert x.dim() == 3

        padding = self._get_padding_for(x)
        inputs = (padding, x) if pad_first else (x, padding)
        # shape: (batch_size, seq_length + window, size)
        res = torch.cat(inputs, dim=1)

        return res

    def _get_padding_for(self, x: Var) -> Var:
        assert x.dim() >= 3
        return Var(x.data.new(x.size(0), self.window, x.size(2)).zero_())


class Concatenate(nn.Module):
    def __init__(self, modules: nn.ModuleList) -> None:
        super(Concatenate, self).__init__()
        self.__modules = modules  # use name-mangling because nn.Module already has modules attribute

    def forward(self, inputs: Sequence[Var]) -> Var:
        assert len(inputs) == len(self.__modules)

        res = [m(i) for m, i in zip(self.__modules, inputs)]
        outputs = torch.cat(res, dim=-1)

        return outputs


class TimeDistributed(nn.Module):
    def __init__(self, module: nn.Module) -> None:
        super(TimeDistributed, self).__init__()
        self.module = module

    def reset_parameters(self) -> None:
        if hasattr(self.module, 'reset_parameters'):
            self.module.reset_parameters()

    def forward(self, inputs: Var) -> Var:
        assert inputs.dim() > 2

        b, t = inputs.size(0), inputs.size(1)

        # shape: (b * t, *r)
        inputs = inputs.view(-1, *inputs.size()[2:])
        # shape: (b * t, *r')
        outputs = self.module(inputs)
        assert outputs.size(0) == b * t
        # shape: (b, t, *r')
        outputs = outputs.view(b, t, *outputs.size()[1:])

        return outputs


class CNNEncoder(nn.Module):
    def __init__(
            self,
            input_size: int,
            num_filters: int = 100,
            filter_width: int = 3,
    ) -> None:
        super(CNNEncoder, self).__init__()
        self.input_size = input_size
        padding = (filter_width - 1, 0)
        self.conv = nn.Conv2d(1, num_filters, (filter_width, input_size), padding=padding)

    def reset_parameters(self) -> None:
        self.conv.reset_parameters()

    def forward(self, inputs: Var) -> Var:
        assert inputs.dim() == 3
        assert inputs.size(-1) == self.input_size

        # shape: (batch_size, 1, seq_length, input_size)
        inputs = inputs.unsqueeze(1)
        # shape: (batch_size, num_filters, seq_length, 1)
        convolved = self.conv(inputs)
        assert convolved.size(-1) == 1
        # shape: (batch_size, num_filters, seq_length)
        convolved = convolved.squeeze(-1)
        # shape: (batch_size, num_filters)
        outputs, _ = convolved.max(dim=-1)

        return outputs


class BiLSTMEmbedder(nn.Module):
    def __init__(
            self,
            embedder: nn.Module,
            embedder_size: int,
            hidden_size: int,
            padding_idx: int = 0,
    ) -> None:
        super(BiLSTMEmbedder, self).__init__()
        self.embedder = embedder
        self.lstm = nn.LSTM(
            embedder_size, hidden_size, num_layers=2, batch_first=True, bidirectional=True)
        self.padding_idx = padding_idx
        self.reset_parameters()

    def reset_parameters(self) -> None:
        if hasattr(self.embedder, 'reset_parameters'):
            self.embedder.reset_parameters()
        self.lstm.reset_parameters()

    def forward(self, inputs: Sequence[Var]) -> Var:
        assert all(i.dim() in (2, 3) for i in inputs)

        # shape: (batch_size, seq_length, embedder_size)
        embedded = self.embedder(inputs)
        # shape: (batch_size, seq_length, N)
        cat = torch.cat([i.unsqueeze(-1) if i.dim() == 2 else i for i in inputs], dim=-1)
        # shape: (batch_size, seq_length)
        mask = torch.sum((cat != self.padding_idx).long(), dim=-1) != 0
        # shape: (batch_size,)
        seq_lengths = torch.sum(mask.long(), dim=1)
        seq_lengths, sent_perm = seq_lengths.sort(0, descending=True)
        # shape: (batch_size, seq_length, embedder_size), sorted by actual seq length
        embedded = embedded[sent_perm]

        packed_input = pack_padded_sequence(
            embedded, seq_lengths.data.cpu().numpy(), batch_first=True)
        lstm_out, _ = self.lstm(packed_input)
        # shape: (batch_size, seq_length, 2 * hidden_size)
        lstm_out, _ = pad_packed_sequence(lstm_out, batch_first=True)
        _, original_perm = sent_perm.sort(0, descending=False)
        # shape: (batch_size, seq_length, 2 * hidden_size), original order
        outputs = lstm_out[original_perm]

        return outputs
