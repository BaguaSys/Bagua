#!/usr/bin/env python3

from bagua.torch_api.dev.bucket import BaguaBucket
from bagua.torch_api.dev.distributed_dev import BaguaModule
from bagua.torch_api.dev.algorithms import Algorithm
from torch.optim.optimizer import Optimizer
import torch
import math

class OnebitAdamAlgorithm(Algorithm):
    def __init__(self, onebit_optimizer: Optimizer, warmup_steps: int, hierarchical_reduce: bool=False):

        self.warmup_steps = warmup_steps
        self.hierarchical_reduce = hierarchical_reduce
        self.optimizer = onebit_optimizer

    def need_reset(self):
        return self.optimizer.step_id == self.warmup_steps

    def init_tensors(self, bagua_module: BaguaModule):
        
        parameters = bagua_module._bagua_build_params()
        
        for name, param in parameters:
           param._one_bit_name = name

        tensor_groups = []            
        for param_group, m_group in zip(self.optimizer.params_in_group, self.optimizer.exp_avgs_in_group):
            group = []
            for param, exp_avgs in zip(param_group, m_group):
                if self.optimizer.step_id < self.warmup_steps:
                    print("Register gradient tensors to the core at step {}".format(self.optimizer.step_id))
                    registered_tensor = param.bagua_ensure_grad().to_bagua_tensor(param._one_bit_name)
                else:
                    print("Register momentum tensors to the core at step {}".format(self.optimizer.step_id))
                    registered_tensor = exp_avgs.to_bagua_tensor(param._one_bit_name)
                    registered_tensor._one_bit_grad = param.bagua_ensure_grad()
                    param._one_bit_momentum = registered_tensor
                group.append(registered_tensor)
            tensor_groups.append(group)

        return tensor_groups

    def init_operations(
            self,
            bagua_module: BaguaModule,
            bucket: BaguaBucket,
    ):
        bucket.backend_bucket.clear_ops()
        if self.optimizer.step_id < self.warmup_steps:
            bucket.backend_bucket.append_centralized_synchronous_op(
                bagua_module.bagua_inter_node_communicator,
                bagua_module.bagua_intra_node_communicator,
                hierarchical=self.hierarchical_reduce,
                average=True,
            )
        else:
            def calculate_momentum(*args):
                beta1, beta2  = self.optimizer.param_groups[0]['betas']
                for tensor in bucket.tensors:
                    tensor.mul_(beta1).add_(tensor._one_bit_grad, alpha=1 - beta1)

            bucket.backend_bucket.append_python_op(calculate_momentum)
            bucket.backend_bucket.append_centralized_synchronous_op(
                bagua_module.bagua_inter_node_communicator,
                bagua_module.bagua_intra_node_communicator,
                hierarchical=self.hierarchical_reduce,
                average=True,
                scattergather=True,
                compression="MinMaxUInt8",
            )

    def init_backward_hook(self, bagua_module: BaguaModule):
        def hook(parameter_name, parameter):
            parameter._one_bit_momentum.bagua_mark_communication_ready()
        return hook


class OnebitAdamOptimizer(Optimizer):
    def __init__(
        self,
        params,
        lr=1e-3,
        warmup_steps=100,
        is_bert=False,
        freeze_test_step=-1,
        betas=(0.9, 0.999),
        eps=1e-8,
        weight_decay=0,
    ):
        if not 0.0 <= lr:
            raise ValueError("Invalid learning rate: {}".format(lr))
        if not 0.0 <= eps:
            raise ValueError("Invalid epsilon value: {}".format(eps))
        if not 0.0 <= betas[0] < 1.0:
            raise ValueError("Invalid beta parameter at index 0: {}".format(betas[0]))
        if not 0.0 <= betas[1] < 1.0:
            raise ValueError("Invalid beta parameter at index 1: {}".format(betas[1]))
        defaults = dict(
            lr=lr, warmup_steps=warmup_steps, is_bert=is_bert, freeze_test_step=freeze_test_step, betas=betas, eps=eps, weight_decay=weight_decay
        )
        super(OnebitAdamOptimizer, self).__init__(params, defaults)

        self.params_in_group = []
        self.exp_avgs_in_group = []
        self.step_id = 0

        ### initialize momentum and variance
        for group_id, group in enumerate(self.param_groups):
            params_with_grad = []
            exp_avgs = []
            for p in group['params']:
                params_with_grad.append(p)
                state = self.state[p]
                if len(state) == 0:
                    state['exp_avg'] = torch.zeros_like(p, memory_format=torch.preserve_format)
                    state['exp_avg_sq'] = torch.zeros_like(p, memory_format=torch.preserve_format)
                exp_avgs.append(state['exp_avg'])
            self.params_in_group.append(params_with_grad)
            self.exp_avgs_in_group.append(exp_avgs)

    def __setstate__(self, state):
        super(OnebitAdam, self).__setstate__(state)

    def step(self, closure=None):
        ## Here we assume grad or state["exp_avg"] have already been updated and averaged.
        ## This step only updates weights.
        self.step_id += 1
        for group_id, group in enumerate(self.param_groups):

            lr = group['lr']
            weight_decay = group['weight_decay']
            beta1, beta2 = group['betas']
            eps = group['eps']

            for param_id, param in enumerate(group['params']):
                state = self.state[param]

                if self.step_id < group["warmup_steps"]:
                    state["exp_avg"].mul_(beta1).add_(param.grad, alpha=1 - beta1)
                    state["exp_avg_sq"].mul_(beta2).addcmul_(param.grad, param.grad, value=1 - beta2)

                bias_correction1 = 1 - beta1 ** self.step_id
                bias_correction2 = 1 - beta2 ** self.step_id

                denom = (
                    state["exp_avg_sq"].sqrt() / math.sqrt(bias_correction2)
                ).add_(eps)
                step_size = lr / bias_correction1
                update = state["exp_avg"] / denom
                param.data.add_(-step_size * update)
        
        

        # loss = None
        # if closure is not None:
        #     with torch.enable_grad():
        #         loss = closure()

        # for group_id, group in enumerate(self.param_groups):

        #     state = self.state[group_id]
        #     state["step"] += 1

        #     lr = group["lr"]
        #     compression_start = group["compression_start"]
        #     is_bert = group["is_bert"]
        #     freeze_test_step = group["freeze_test_step"]

        #     beta1, beta2 = group["betas"]
        #     eps = group["eps"]
        #     weight_decay = group["weight_decay"]

        #     for param in group["params"]:

        #         param_weights = param.data
        #         param_grads = param.grad

        #         # no compression
        #         if (
        #             state["step"] < compression_start
        #             or state["group_numel"] < 8 * self.size
        #             or freeze_test_step > 0
        #         ):
        #             # allreduce grads
        #             communicator.allreduce_tensor(param_grads, param_grads, cupy_stream)
        #             param_grads.div_(self.size)

        #             if not is_bert:
        #                 if weight_decay != 0:
        #                     param_grads.add_(param_weights, alpha=weight_decay)

        #             state["exp_avg"].mul_(beta1).add_(param_grads, alpha=1 - beta1)

        #             # if freeze exp_avg_sq
        #             if (
        #                 freeze_test_step > 0
        #                 and state["step"] >= freeze_test_step
        #             ):
        #                 # freeze exp_avg_sq for testing
        #                 if state["step"] == freeze_test_step:
        #                     print(
        #                         "Rank{} 1bit-adam freeze test starts from the step {}".format(
        #                             self.rank, freeze_test_step
        #                         )
        #                     )
        #                 pass
        #             else:
        #                 # update exp_avg_sq as normal
        #                 state["exp_avg_sq"].mul_(beta2).addcmul_(param_grads, param_grads, value=1 - beta2)

        #         # with compression
        #         else:

        #             if state["step"] == compression_start:
        #                 print(
        #                     "Rank{} 1bit-adam quantization starts from the step {}".format(
        #                         self.rank,
        #                         compression_start,
        #                     )
        #                 )

        #             if not is_bert:
        #                 if weight_decay != 0:
        #                     param_grads.add_(param_weights, alpha=weight_decay)

        #             state["exp_avg"].mul_(beta1).add_(param_grads, alpha=1 - beta1)

        #             aggregated_exp_avg = cen_lowprec_sync(
        #                 state["exp_avg"],
        #                 onebit_quantize,
        #                 onebit_dequantize,
        #                 communicator,
        #                 cupy_stream,
        #                 state["worker_error"],
        #                 state["server_error"],
        #             )

        #             state["exp_avg"].copy_(aggregated_exp_avg)

                
        #         ## communication is finished.
        #         ## update param.
        #         if is_bert:
        #             update = state["exp_avg"] / (state["exp_avg_sq"].sqrt() + eps)
        #             if weight_decay != 0:
        #                 update += weight_decay * param_weights
        #             param_weights.add_(-lr * update)
        #         else:
        #             bias_correction1 = 1 - beta1 ** state["step"]
        #             bias_correction2 = 1 - beta2 ** state["step"]

        #             denom = (
        #                 state["exp_avg_sq"].sqrt() / math.sqrt(bias_correction2)
        #             ).add_(eps)
        #             step_size = lr / bias_correction1
        #             update = state["exp_avg"] / denom

        #             param_weights.add_(-step_size * update)

        # return loss
