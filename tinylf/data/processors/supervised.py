import bisect
from collections import defaultdict
from typing import Any, Dict, List, Optional, Sequence, Tuple

from loguru import logger
from transformers import PreTrainedTokenizer, ProcessorMixin
from ...params import DataArguments
from ..template import Template


IGNORE_INDEX = -100


def search_for_fit(numbers: Sequence[int], capacity: int) -> int:
    r"""
    Finds the index of largest number that fits into the knapsack with the given capacity.
    """
    index = bisect.bisect(numbers, capacity)
    return -1 if index == 0 else (index - 1)


def greedy_knapsack(numbers: List[int], capacity: int) -> List[List[int]]:
    r"""
    An efficient greedy algorithm with binary search for the knapsack problem.
    """
    numbers.sort()  # sort numbers in ascending order for binary search
    knapsacks = []

    while numbers:
        current_knapsack = []
        remaining_capacity = capacity

        while True:
            index = search_for_fit(numbers, remaining_capacity)
            if index == -1:
                break  # no more numbers fit in this knapsack

            remaining_capacity -= numbers[index]  # update the remaining capacity
            current_knapsack.append(numbers.pop(index))  # add the number to knapsack

        knapsacks.append(current_knapsack)

    return knapsacks


def infer_seqlen(source_len: int, target_len: int, cutoff_len: int) -> Tuple[int, int]:
    r"""
    Computes the real sequence length after truncation by the cutoff_len.
    """
    if target_len * 2 < cutoff_len:  # truncate source
        max_target_len = cutoff_len
    elif source_len * 2 < cutoff_len:  # truncate target
        max_target_len = cutoff_len - source_len
    else:  # truncate both
        max_target_len = int(cutoff_len * (target_len / (source_len + target_len)))

    new_target_len = min(max_target_len, target_len)
    max_source_len = max(cutoff_len - new_target_len, 0)
    new_source_len = min(max_source_len, source_len)
    return new_source_len, new_target_len


def _encode_supervised_example(
    prompt: Sequence[Dict[str, str]],
    response: Sequence[Dict[str, str]],
    system: Optional[str],
    template: "Template",
    tokenizer: "PreTrainedTokenizer",
    cutoff_len: int,
    train_on_prompt: bool,
    mask_history: bool,
) -> Tuple[List[int], List[int]]:
    messages = prompt + response
    input_ids, labels = [], []
    encoded_pairs = template.encode_multiturn(tokenizer, messages, system)
    total_length = len(input_ids) + (1 if template.efficient_eos else 0)
    if mask_history:
        encoded_pairs = encoded_pairs[::-1]  # high priority for last turns

    for turn_idx, (source_ids, target_ids) in enumerate(encoded_pairs):
        if total_length >= cutoff_len:
            break

        # 推算实际的source和target len
        source_len, target_len = infer_seqlen(len(source_ids), len(target_ids), cutoff_len - total_length)
        source_ids = source_ids[:source_len]
        target_ids = target_ids[:target_len]
        total_length += source_len + target_len

        if train_on_prompt:
            source_label = source_ids
        elif template.efficient_eos:
            source_label = [tokenizer.eos_token_id] + [IGNORE_INDEX] * (source_len - 1)
        else:
            source_label = [IGNORE_INDEX] * source_len # 忽略source_label

        if mask_history and turn_idx != 0:  # train on the last turn only
            target_label = [IGNORE_INDEX] * target_len
        else:
            target_label = target_ids

        if mask_history:  # reversed sequences
            input_ids = source_ids + target_ids + input_ids
            labels = source_label + target_label + labels
        else:
            input_ids += source_ids + target_ids
            labels += source_label + target_label

    if template.efficient_eos:
        input_ids += [tokenizer.eos_token_id]
        labels += [tokenizer.eos_token_id]

    return input_ids, labels


def preprocess_supervised_dataset(
    examples: Dict[str, List[Any]],
    template: "Template",
    tokenizer: "PreTrainedTokenizer",
    data_args: "DataArguments",
) -> Dict[str, List[Any]]:
    # build inputs with format `<bos> X Y <eos>` and labels with format `<ignore> ... <ignore> Y <eos>`
    # for multiturn examples, we only mask the prompt part in each prompt-response pair.
    model_inputs = defaultdict(list)
    for i in range(len(examples["_prompt"])):  # dataset.map(batched=True)
        # TODO: 这一坨代码实际上在dataset_format阶段就可以做校验
        if len(examples["_prompt"][i]) % 2 != 1 or len(examples["_response"][i]) != 1:
            logger.warning(
                "Dropped invalid example: {}".format(examples["_prompt"][i] + examples["_response"][i])
            )
            continue

        input_ids, labels = _encode_supervised_example(
            prompt=examples["_prompt"][i],
            response=examples["_response"][i],
            system=examples["_system"][i],
            template=template,
            tokenizer=tokenizer,
            cutoff_len=data_args.cutoff_len,
            train_on_prompt=data_args.train_on_prompt,
            mask_history=data_args.mask_history,
        )
        model_inputs["input_ids"].append(input_ids)
         # attention_mask 1标识对应ids有效，0标识是padding的无效ids
        model_inputs["attention_mask"].append([1] * len(input_ids))
        model_inputs["labels"].append(labels)

    return model_inputs


def preprocess_packed_supervised_dataset(
    examples: Dict[str, List[Any]],
    template: "Template",
    tokenizer: "PreTrainedTokenizer",
    # processor: Optional["ProcessorMixin"],
    data_args: "DataArguments",
) -> Dict[str, List[Any]]:
    # TODO: use `position_ids` to achieve packing
    # build inputs with format `<bos> X1 Y1 <eos> <bos> X2 Y2 <eos>`
    # and labels with format `<ignore> ... <ignore> Y1 <eos> <ignore> ... <ignore> Y2 <eos>`
    valid_num = 0
    batch_input_ids, batch_labels, batch_images, batch_videos = [], [], [], []
    lengths = []
    length2indexes = defaultdict(list)
    for i in range(len(examples["_prompt"])):
        if len(examples["_prompt"][i]) % 2 != 1 or len(examples["_response"][i]) != 1:
            logger.warning(
                "Dropped invalid example: {}".format(examples["_prompt"][i] + examples["_response"][i])
            )
            continue

        input_ids, labels = _encode_supervised_example(
            prompt=examples["_prompt"][i],
            response=examples["_response"][i],
            system=examples["_system"][i],
            tools=examples["_tools"][i],
            images=examples["_images"][i] or [],
            videos=examples["_videos"][i] or [],
            template=template,
            tokenizer=tokenizer,
            cutoff_len=data_args.cutoff_len - 1,  # reserved for the padding token
            train_on_prompt=data_args.train_on_prompt,
            mask_history=data_args.mask_history,
        )
        length = len(input_ids)
        if length > data_args.cutoff_len:
            logger.warning_rank0(f"Dropped lengthy example with length {length} > {data_args.cutoff_len}.")
        else:
            lengths.append(length)
            length2indexes[length].append(valid_num)
            batch_input_ids.append(input_ids)
            batch_labels.append(labels)
            batch_images.append(examples["_images"][i] or [])
            batch_videos.append(examples["_videos"][i] or [])
            valid_num += 1

    model_inputs = defaultdict(list)
    knapsacks = greedy_knapsack(lengths, data_args.cutoff_len - 1)  # reserved for the padding token
    for knapsack in knapsacks:
        packed_input_ids, packed_attention_masks, packed_labels = [], [], []
        packed_images, packed_videos = [], []
        for i, length in enumerate(knapsack):
            index = length2indexes[length].pop()
            packed_input_ids += batch_input_ids[index]
            packed_labels += batch_labels[index]
            packed_images += batch_images[index]
            packed_videos += batch_videos[index]
            if data_args.neat_packing:
                packed_attention_masks += [i + 1] * len(batch_input_ids[index])  # start from 1
            else:
                packed_attention_masks += [1] * len(batch_input_ids[index])

        if len(packed_input_ids) < data_args.cutoff_len:
            pad_length = data_args.cutoff_len - len(packed_input_ids)
            packed_input_ids += [tokenizer.pad_token_id] * pad_length
            packed_labels += [IGNORE_INDEX] * pad_length
            if data_args.neat_packing:
                packed_attention_masks += [0] * pad_length
            else:
                packed_attention_masks += [1] * pad_length  # more efficient flash_attn

        if len(packed_input_ids) != data_args.cutoff_len:
            raise ValueError("The length of packed example should be identical to the cutoff length.")

        model_inputs["input_ids"].append(packed_input_ids)
        model_inputs["attention_mask"].append(packed_attention_masks)
        model_inputs["labels"].append(packed_labels)
        model_inputs["images"].append(packed_images or None)
        model_inputs["videos"].append(packed_videos or None)

    return model_inputs


def print_supervised_dataset_example(example: Dict[str, List[int]], tokenizer: "PreTrainedTokenizer") -> None:
    valid_labels = list(filter(lambda x: x != IGNORE_INDEX, example["labels"]))
    print("input_ids:\n{}".format(example["input_ids"]))
    print("inputs:\n{}".format(tokenizer.decode(example["input_ids"], skip_special_tokens=False)))
    print("label_ids:\n{}".format(example["labels"]))
    print(f"labels:\n{tokenizer.decode(valid_labels, skip_special_tokens=False)}")
