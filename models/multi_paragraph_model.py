# -*- coding: utf-8 -*-
import torch
from torch import nn
from torch.autograd import Variable
import torch.nn.functional as F
from bidaf import BiDAF
from torch.nn import Parameter
from functools import wraps

def masked_log_softmax(vector, mask):
    """
    ``torch.nn.functional.log_softmax(vector)`` does not work if some elements of ``vector`` should be
    masked.  This performs a log_softmax on just the non-masked portions of ``vector``.  Passing
    ``None`` in for the mask is also acceptable; you'll just get a regular log_softmax.
    We assume that both ``vector`` and ``mask`` (if given) have shape ``(batch_size, vector_dim)``.
    In the case that the input vector is completely masked, the return value of this function is
    arbitrary, but not ``nan``.  You should be masking the result of whatever computation comes out
    of this in that case, anyway, so the specific values returned shouldn't matter.  Also, the way
    that we deal with this case relies on having single-precision floats; mixing half-precision
    floats with fully-masked vectors will likely give you ``nans``.
    If your logits are all extremely negative (i.e., the max value in your logit vector is -50 or
    lower), the way we handle masking here could mess you up.  But if you've got logit values that
    extreme, you've got bigger problems than this.
    """
    if mask is not None:
        # vector + mask.log() is an easy way to zero out masked elements in logspace, but it
        # results in nans when the whole vector is masked.  We need a very small value instead of a
        # zero in the mask for these cases.  log(1 + 1e-45) is still basically 0, so we can safely
        # just add 1e-45 before calling mask.log().  We use 1e-45 because 1e-46 is so small it
        # becomes 0 - this is just the smallest value we can actually use.
        vector = vector + (mask + 1e-45).log()
    return torch.nn.functional.log_softmax(vector, dim=1)

def masked_softmax(vector, mask):
	"""
	``torch.nn.functional.softmax(vector)`` does not work if some elements of ``vector`` should be
	masked.  This performs a softmax on just the non-masked portions of ``vector``.  Passing
	``None`` in for the mask is also acceptable; you'll just get a regular softmax.
	We assume that both ``vector`` and ``mask`` (if given) have shape ``(batch_size, vector_dim)``.
	In the case that the input vector is completely masked, this function returns an array
	of ``0.0``. This behavior may cause ``NaN`` if this is used as the last layer of a model
	that uses categorical cross-entropy loss.
	"""
	if mask is None:
		result = F.softmax(vector, dim=-1)
	else:
		# To limit numerical errors from large vector elements outside the mask, we zero these out.
		result	 = torch.nn.functional.softmax(vector * mask, dim=-1)
		result = result * mask
		result = result / (result.sum(dim=1, keepdim=True) + 1e-13)
	return result


def masked_log_softmax(vector, mask):
    """
    ``torch.nn.functional.log_softmax(vector)`` does not work if some elements of ``vector`` should be
    masked.  This performs a log_softmax on just the non-masked portions of ``vector``.  Passing
    ``None`` in for the mask is also acceptable; you'll just get a regular log_softmax.
    We assume that both ``vector`` and ``mask`` (if given) have shape ``(batch_size, vector_dim)``.
    In the case that the input vector is completely masked, the return value of this function is
    arbitrary, but not ``nan``.  You should be masking the result of whatever computation comes out
    of this in that case, anyway, so the specific values returned shouldn't matter.  Also, the way
    that we deal with this case relies on having single-precision floats; mixing half-precision
    floats with fully-masked vectors will likely give you ``nans``.
    If your logits are all extremely negative (i.e., the max value in your logit vector is -50 or
    lower), the way we handle masking here could mess you up.  But if you've got logit values that
    extreme, you've got bigger problems than this.
    """
    if mask is not None:
        # vector + mask.log() is an easy way to zero out masked elements in logspace, but it
        # results in nans when the whole vector is masked.  We need a very small value instead of a
        # zero in the mask for these cases.  log(1 + 1e-45) is still basically 0, so we can safely
        # just add 1e-45 before calling mask.log().  We use 1e-45 because 1e-46 is so small it
        # becomes 0 - this is just the smallest value we can actually use.
        vector = vector + (mask + 1e-45).log()
    return torch.nn.functional.log_softmax(vector, dim=1)

def masked_log_softmax_global(vector, mask):
	input_flatten = vector.contiguous().view(-1)  # flatten
	mask_flatten = mask.contiguous().view(-1)  # flatten
	if mask is not None:
		input_flatten = input_flatten + (mask_flatten + 1e-45).log()
		result = torch.nn.functional.log_softmax(input_flatten, dim=0)
		return result.unsqueeze(0)


def replace_masked_values(tensor, mask, replace_with):
	"""
	Replaces all masked values in ``tensor`` with ``replace_with``.  ``mask`` must be broadcastable
	to the same shape as ``tensor``. We require that ``tensor.dim() == mask.dim()``, as otherwise we
	won't know which dimensions of the mask to unsqueeze.
	"""
	# We'll build a tensor of the same shape as `tensor`, zero out masked values, then add back in
	# the `replace_with` value.
	if tensor.dim() != mask.dim():
		raise Exception("tensor.dim() (%d) != mask.dim() (%d)" % (tensor.dim(), mask.dim()))
	one_minus_mask = 1.0 - mask
	values_to_add = replace_with * one_minus_mask
	return tensor * mask + values_to_add


class MultiParagraph(nn.Module):
	def __init__(self, args, loader):
		super(MultiParagraph, self).__init__()
		hidden_size = args.hidden_size
		embed_size = args.embed_size
		word_vocab_size = loader.vocab.get_length()

		GRU_hidden_size = hidden_size



		if args.dropout > 0:
			self._dropout = torch.nn.Dropout(p=args.dropout)
		else:
			self._dropout = lambda x: x

		## word embedding layer
		self.word_embedding_layer = LookupEncoder(word_vocab_size, embedding_dim=embed_size)

		## contextual embedding layer
		self.contextual_embedding_layer = RecurrentContext(input_size=embed_size, hidden_size=GRU_hidden_size, num_layers=1)

		## bidirectional attention flow between question and context
		self.attention_flow_layer1 = BiDAF(2*GRU_hidden_size)

		linearLayer_dim = 8 * GRU_hidden_size
		linear_layer_output_dim = 2 * GRU_hidden_size
		self.linearLayer = TimeDistributed(nn.Sequential(
			torch.nn.Linear(linearLayer_dim, linear_layer_output_dim),
			torch.nn.ReLU()))

		## modelling layer for question and context : this layer also converts the 8 dimensional input intp two dimensioanl output
		modeling_layer_inputdim = linear_layer_output_dim
		self.modeling_layer1 = RecurrentContext(modeling_layer_inputdim, GRU_hidden_size)

		## bidirectional attention flow between [q+c] and answer
		self.attention_flow_layer2 = BiDAF(2*GRU_hidden_size)

		self.linearLayer_2 = TimeDistributed(nn.Sequential(
			torch.nn.Linear(linearLayer_dim, linear_layer_output_dim),
			torch.nn.ReLU()))

		## modeling layer_2
		modeling_layer_inputdim = 2*GRU_hidden_size
		self.modeling_layer2 = RecurrentContext(modeling_layer_inputdim, GRU_hidden_size)

		span_start_input_dim = 2 * GRU_hidden_size
		self._span_start_predictor = TimeDistributed(torch.nn.Linear(span_start_input_dim, 1))


		span_end_input_dim = (2 * GRU_hidden_size) + (2 * GRU_hidden_size)
		self._span_end_encoder = RecurrentContext(span_end_input_dim, GRU_hidden_size, num_layers=1)

		span_end_dim = (2* GRU_hidden_size)
		self._span_end_predictor = TimeDistributed(torch.nn.Linear(span_end_dim, 1))


		self._span_start_accuracy = Accuracy()
		self._span_end_accuracy = Accuracy()
		self._span_accuracy = BooleanAccuracy()

		self._span_start_accuracy_valid = Accuracy()
		self._span_end_accuracy_valid = Accuracy()
		self._span_accuracy_valid = BooleanAccuracy()

	def forward(self, batch_query, batch_query_length,batch_question_mask,
					batch_context, batch_context_length, batch_context_mask,batch_context_unsort,
				span_start, span_end,identity_context):

		## Embed query and context
		query_embedded = self.word_embedding_layer(batch_query.unsqueeze(0))  # (N, J, d)
		query_embedded  = self._dropout(query_embedded)

		context_embedded = self.word_embedding_layer(batch_context) # (N, T, d)
		context_embedded = self._dropout(context_embedded)

		num_passages= context_embedded.size(0)
		batch_question_mask = batch_question_mask.expand(num_passages, -1)

		## Encode query and context
		query_encoded,_ = self.contextual_embedding_layer(query_embedded, batch_query_length)  # (N, J, 2d)
		query_encoded = self._dropout(query_encoded)

		query_encoded = query_encoded.expand(num_passages, query_encoded.size(1), query_encoded.size(2))
		context_encoded,_ = self.contextual_embedding_layer(context_embedded, batch_context_length) # (N, T, 2d)
		context_encoded = self._dropout(context_encoded)

		## BiDAF 1 to get ~U, ~h and G (8d) between context and query
		# (N, T, 8d) , (N, T ,2d) , (N, 1, 2d)

		context_attention_encoded, query_aware_context_encoded, context_aware_query_encoded = self.attention_flow_layer1(query_encoded, context_encoded,batch_question_mask,batch_context_mask)
		context_attention_encoded = self._dropout(context_attention_encoded)

		context_attention_encoded_LR = self.linearLayer(context_attention_encoded)  #(N, T, ld)
		context_attention_encoded_LR = self._dropout(context_attention_encoded_LR)

		## modelling layer 1
		context_modeled,_ = self.modeling_layer1(context_attention_encoded_LR, batch_context_length)  # (N, T, ld) => (N, T, 2d)
		context_modeled = self._dropout(context_modeled)
		prediction = context_modeled
		context_final_encoded = context_attention_encoded_LR
		##self-attention
		'''
		context_post_self_attn,_,_ = self.attention_flow_layer2(context_modeled,context_modeled, batch_context_mask,batch_context_mask, direction=True, identity=identity_context)
		context_self_attention_encoded_LR = self.linearLayer_2(context_post_self_attn)  # (N, T, ld)

		context_final_encoded = context_self_attention_encoded_LR + context_attention_encoded_LR  # (N, T, ld)
		context_final_encoded = self._dropout(context_final_encoded)
		

		prediction,_ = self.modeling_layer2(context_final_encoded, batch_context_length)    # (N, T, 2*GRU_hidden_size)
		prediction  = self._dropout(prediction)  # Shape: (batch_size, passage_length, 2*GRU_hidden_size))
		'''
		# Start prediction
		span_start_logits = self._span_start_predictor(prediction).squeeze(-1)  # Shape: (batch_size, passage_length)
		span_start_logits = replace_masked_values(span_start_logits, batch_context_mask, -1e7)

		span_end_representation = torch.cat([context_final_encoded,prediction],dim=2)   # Shape: (batch_size, passage_length, 2*GRU+hidden_dim)
		span_end_representation  = self._dropout(span_end_representation)

		encoded_span_end, _ = self._span_end_encoder(span_end_representation, batch_context_length)
		encoded_span_end = self._dropout(encoded_span_end)

		span_end_logits = self._span_end_predictor(encoded_span_end).squeeze(-1)
		span_end_logits = replace_masked_values(span_end_logits, batch_context_mask, -1e7)


		flattened_span_start_logits = span_start_logits.contiguous().view(-1).unsqueeze(0)
		flattened_span_end_logits = span_end_logits.contiguous().view(-1).unsqueeze(0)
		best_span = self.get_best_span(flattened_span_start_logits, flattened_span_end_logits)

		# Compute the loss for training.
		if span_start is not None:
			#loss = F.nll_loss(masked_log_softmax(span_start_logits, batch_context_mask), span_start.squeeze(-1))
			loss = F.nll_loss(masked_log_softmax_global(span_start_logits, batch_context_mask), span_start.squeeze(-1))
			self._span_start_accuracy.accuracy(flattened_span_start_logits, span_start.squeeze(-1))

			#loss += F.nll_loss(masked_log_softmax(span_end_logits, batch_context_mask), span_end.squeeze(-1))
			loss += F.nll_loss(masked_log_softmax_global(span_end_logits, batch_context_mask), span_end.squeeze(-1))

			self._span_end_accuracy.accuracy(flattened_span_end_logits, span_end.squeeze(-1))
			self._span_accuracy.accuracy(best_span, torch.stack([span_start, span_end], -1))

			return loss, self._span_start_accuracy.correct_count, self._span_end_accuracy.correct_count, self._span_accuracy._correct_count

	def eval(self,batch_query, batch_query_length,batch_question_mask,
			 batch_context, batch_context_length, batch_context_mask,
				      span_start, span_end,identity_context):
		## Embed query and context
		query_embedded = self.word_embedding_layer(batch_query.unsqueeze(0))  # (N, J, d)
		context_embedded = self.word_embedding_layer(batch_context)  # (N, T, d)

		num_passages = context_embedded.size(0)
		batch_question_mask = batch_question_mask.expand(num_passages, -1)

		## Encode query and context
		query_encoded, _ = self.contextual_embedding_layer(query_embedded, batch_query_length)  # (N, J, 2d)
		query_encoded = query_encoded.expand(num_passages, query_encoded.size(1), query_encoded.size(2))
		context_encoded, _ = self.contextual_embedding_layer(context_embedded, batch_context_length)  # (N, T, 2d)

		## BiDAF 1 to get ~U, ~h and G (8d) between context and query
		# (N, T, 8d) , (N, T ,2d) , (N, 1, 2d)

		context_attention_encoded, query_aware_context_encoded, context_aware_query_encoded = self.attention_flow_layer1(
			query_encoded, context_encoded, batch_question_mask, batch_context_mask)

		context_attention_encoded_LR = self.linearLayer(context_attention_encoded)  # (N, T, ld)

		## modelling layer 1
		context_modeled, _ = self.modeling_layer1(context_attention_encoded_LR,
												  batch_context_length)  # (N, T, ld) => (N, T, 2d)

		prediction = context_modeled
		context_final_encoded = context_attention_encoded_LR
		'''
		##self-attention
		context_post_self_attn, _, _ = self.attention_flow_layer2(context_modeled, context_modeled, batch_context_mask,
																  batch_context_mask, direction=True,
																  identity=identity_context)
		context_self_attention_encoded_LR = self.linearLayer_2(context_post_self_attn)  # (N, T, ld)

		context_final_encoded = context_self_attention_encoded_LR + context_attention_encoded_LR  # (N, T, ld)

		prediction, _ = self.modeling_layer2(context_final_encoded, batch_context_length)  # (N, T, 2*GRU_hidden_size)
		prediction = self._dropout(prediction)  # Shape: (batch_size, passage_length, 2*GRU_hidden_size))
		'''
		# Start prediction
		span_start_logits = self._span_start_predictor(prediction).squeeze(-1)  # Shape: (batch_size, passage_length)
		span_start_logits = replace_masked_values(span_start_logits, batch_context_mask, -1e7)

		span_end_representation = torch.cat([context_final_encoded, prediction],
											dim=2)  # Shape: (batch_size, passage_length, 2*GRU+hidden_dim)
		encoded_span_end, _ = self._span_end_encoder(span_end_representation, batch_context_length)
		encoded_span_end = self._dropout(encoded_span_end)
		span_end_logits = self._span_end_predictor(encoded_span_end).squeeze(-1)
		span_end_logits = replace_masked_values(span_end_logits, batch_context_mask, -1e7)

		flattened_span_start_logits = span_start_logits.contiguous().view(-1).unsqueeze(0)
		flattened_span_end_logits = span_end_logits.contiguous().view(-1).unsqueeze(0)
		best_span = self.get_best_span(flattened_span_start_logits, flattened_span_end_logits)

		# Compute the loss for training.
		if span_start is not None:
			self._span_start_accuracy.accuracy(flattened_span_start_logits, span_start.squeeze(-1))
			self._span_end_accuracy.accuracy(flattened_span_end_logits, span_end.squeeze(-1))
			self._span_accuracy.accuracy(best_span, torch.stack([span_start, span_end], -1))

			return self._span_start_accuracy.correct_count, self._span_end_accuracy.correct_count, self._span_accuracy._correct_count

	def get_best_span(self,span_start_logits, span_end_logits):
		if span_start_logits.dim() != 2 or span_end_logits.dim() != 2:
			raise ValueError("Input shapes must be (batch_size, passage_length)")

		batch_size, passage_length = span_start_logits.size()
		max_span_log_prob = [-1e20] * batch_size
		span_start_argmax = [0] * batch_size
		best_word_span = Variable(span_start_logits.data.new()
								  .resize_(batch_size, 2).fill_(0)).long()

		span_start_logits = span_start_logits.data.cpu().numpy()
		span_end_logits = span_end_logits.data.cpu().numpy()

		for b in range(batch_size):  # pylint: disable=invalid-name
			for j in range(passage_length):
				val1 = span_start_logits[b, span_start_argmax[b]]
				if val1 < span_start_logits[b, j]:
					span_start_argmax[b] = j
					val1 = span_start_logits[b, j]

				val2 = span_end_logits[b, j]

				if val1 + val2 > max_span_log_prob[b]:
					best_word_span[b, 0] = span_start_argmax[b]
					best_word_span[b, 1] = j
					max_span_log_prob[b] = val1 + val2
		return best_word_span

class RecurrentContext(nn.Module):
	def __init__(self, input_size, hidden_size, num_layers=1):
		# format of input output
		super(RecurrentContext, self).__init__()
		self.lstm_layer = nn.LSTM(input_size=input_size, hidden_size=hidden_size, num_layers=num_layers,
								  bidirectional=True, batch_first=True)

	def forward(self, batch, batch_length):
		packed = torch.nn.utils.rnn.pack_padded_sequence(batch, batch_length, batch_first=True)
		self.lstm_layer.flatten_parameters()
		outputs, hidden = self.lstm_layer(packed)  # output: concatenated hidden dimension
		outputs_unpacked, _ = torch.nn.utils.rnn.pad_packed_sequence(outputs, batch_first=True)
		return outputs_unpacked, hidden


class LookupEncoder(nn.Module):
	def __init__(self, vocab_size, embedding_dim, pretrain_embedding=None):
		super(LookupEncoder, self).__init__()
		self.embedding_dim = embedding_dim
		self.word_embeddings = nn.Embedding(vocab_size, embedding_dim)
		if pretrain_embedding is not None:
			self.word_embeddings.weight.data.copy_(torch.from_numpy(pretrain_embedding))
			self.word_embeddings.weight.requires_grad = False

	def forward(self, batch):
		return self.word_embeddings(batch)

class TimeDistributed(torch.nn.Module):
	"""
    Given an input shaped like ``(batch_size, time_steps, [rest])`` and a ``Module`` that takes
    inputs like ``(batch_size, [rest])``, ``TimeDistributed`` reshapes the input to be
    ``(batch_size * time_steps, [rest])``, applies the contained ``Module``, then reshapes it back.
    Note that while the above gives shapes with ``batch_size`` first, this ``Module`` also works if
    ``batch_size`` is second - we always just combine the first two dimensions, then split them.
    """
	def __init__(self, module):
		super(TimeDistributed, self).__init__()
		self._module = module

	def forward(self, *inputs):  # pylint: disable=arguments-differ
		reshaped_inputs = []
		for input_tensor in inputs:
			input_size = input_tensor.size()
			if len(input_size) <= 2:
				raise RuntimeError("No dimension to distribute: " + str(input_size))

			# Squash batch_size and time_steps into a single axis; result has shape
			#  (batch_size * time_steps, input_size).
			squashed_shape = [-1] + [x for x in input_size[2:]]
			reshaped_inputs.append(input_tensor.contiguous().view(*squashed_shape))

		reshaped_outputs = self._module(*reshaped_inputs)

		# Now get the output back into the right shape.
        # (batch_size, time_steps, [hidden_size])
		new_shape = [input_size[0], input_size[1]] + [x for x in reshaped_outputs.size()[1:]]
		outputs = reshaped_outputs.contiguous().view(*new_shape)

		return outputs


class WeightDrop(torch.nn.Module):
    def __init__(self, module, weights, dropout=0, variational=False):
        super(WeightDrop, self).__init__()
        self.module = module
        self.weights = weights
        self.dropout = dropout
        self.variational = variational
        self._setup()

    def widget_demagnetizer_y2k_edition(*args, **kwargs):
        # We need to replace flatten_parameters with a nothing function
        # It must be a function rather than a lambda as otherwise pickling explodes
        # We can't write boring code though, so ... WIDGET DEMAGNETIZER Y2K EDITION!
        # (╯°□°）╯︵ ┻━┻
        return

    def _setup(self):
        # Terrible temporary solution to an issue regarding compacting weights re: CUDNN RNN
        if issubclass(type(self.module), torch.nn.RNNBase):
            self.module.flatten_parameters = self.widget_demagnetizer_y2k_edition

        for name_w in self.weights:
            print('Applying weight drop of {} to {}'.format(self.dropout, name_w))
            w = getattr(self.module, name_w)
            del self.module._parameters[name_w]
            self.module.register_parameter(name_w + '_raw', Parameter(w.data))

    def _setweights(self):
        for name_w in self.weights:
            raw_w = getattr(self.module, name_w + '_raw')
            w = None
            if self.variational:
                mask = torch.autograd.Variable(torch.ones(raw_w.size(0), 1))
                if raw_w.is_cuda: mask = mask.cuda()
                mask = torch.nn.functional.dropout(mask, p=self.dropout, training=True)
                w = mask.expand_as(raw_w) * raw_w
            else:
                w = torch.nn.functional.dropout(raw_w, p=self.dropout, training=self.training)
            setattr(self.module, name_w, w)

    def forward(self, *args):
        self._setweights()
        return self.module.forward(*args)


class Accuracy:
	def __init__(self, top_k=1):
		self.top_k = top_k
		self.correct_count = 0.0
		self.total_count = 0.0

	def unwrap_to_tensors(*tensors):
		"""
	    If you actually passed in Variables to a Metric instead of Tensors, there will be
	    a huge memory leak, because it will prevent garbage collection for the computation
	    graph. This method ensures that you're using tensors directly and that they are on
	    the CPU.
	    """
		return (x.data.cpu() if isinstance(x, torch.autograd.Variable) else x for x in tensors)

	def accuracy(self, predictions, gold_labels):
		# Get the data from the Variables.
		_, predictions, gold_labels = self.unwrap_to_tensors(predictions, gold_labels)

		# Some sanity checks.
		num_classes = predictions.size(-1)

		if gold_labels.dim() != predictions.dim() - 1:
			raise Exception("gold_labels must have dimension == predictions.size() - 1 but "
							"found tensor of shape: {}".format(predictions.size()))

		if (gold_labels >= num_classes).any():
			raise Exception("A gold label passed to Categorical Accuracy contains an id >= {}, "
							"the number of classes.".format(num_classes))

		# Top K indexes of the predictions (or fewer, if there aren't K of them).
		# Special case topk == 1, because it's common and .max() is much faster than .topk().
		if self.top_k == 1:
			top_k = predictions.max(-1)[1].unsqueeze(-1)
		else:
			top_k = predictions.topk(min(self.top_k, predictions.shape[-1]), -1)[1]

		# This is of shape (batch_size, ..., top_k).
		correct = top_k.eq(gold_labels.long().unsqueeze(-1)).float()

		self.total_count += gold_labels.numel()
		self.correct_count += correct.sum()

	def get_metric(self, reset=False):
		"""
        Returns
        -------
        The accumulated accuracy.
        """
		accuracy = float(self.correct_count) / float(self.total_count)
		if reset:
			self.reset()
		return accuracy

	def reset(self):
		self.correct_count = 0.0
		self.total_count = 0.0


class BooleanAccuracy():
	"""
    Just checks batch-equality of two tensors and computes an accuracy metric based on that.  This
    is similar to :class:`CategoricalAccuracy`, if you've already done a ``.max()`` on your
    predictions.  If you have categorical output, though, you should typically just use
    :class:`CategoricalAccuracy`.  The reason you might want to use this instead is if you've done
    some kind of constrained inference and don't have a prediction tensor that matches the API of
    :class:`CategoricalAccuracy`, which assumes a final dimension of size ``num_classes``.
    """

	def __init__(self):
		self._correct_count = 0.
		self._total_count = 0.

	def unwrap_to_tensors(*tensors):
		"""
	    If you actually passed in Variables to a Metric instead of Tensors, there will be
	    a huge memory leak, because it will prevent garbage collection for the computation
	    graph. This method ensures that you're using tensors directly and that they are on
	    the CPU.
	    """
		return (x.data.cpu() if isinstance(x, torch.autograd.Variable) else x for x in tensors)

	def accuracy(self,
				 predictions,
				 gold_labels):
		"""
        Parameters
        ----------
        predictions : ``torch.Tensor``, required.
            A tensor of predictions of shape (batch_size, ...).
        gold_labels : ``torch.Tensor``, required.
            A tensor of the same shape as ``predictions``.
        mask: ``torch.Tensor``, optional (default = None).
            A tensor of the same shape as ``predictions``.
        """
		# Get the data from the Variables.
		_, predictions, gold_labels = self.unwrap_to_tensors(predictions, gold_labels)
		batch_size = predictions.size(0)
		predictions = predictions.view(batch_size, -1)
		gold_labels = gold_labels.view(batch_size, -1)

		# The .prod() here is functioning as a logical and.
		correct = predictions.eq(gold_labels).prod(dim=1).float()
		count = torch.ones(gold_labels.size(0))
		self._correct_count += correct.sum()
		self._total_count += count.sum()

	def get_metric(self, reset=False):
		accuracy = float(self._correct_count) / float(self._total_count)
		if reset:
			self.reset()
		return accuracy

	def reset(self):
		self._correct_count = 0.0
		self._total_count = 0.0