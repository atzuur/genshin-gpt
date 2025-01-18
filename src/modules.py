import math
import torch
import torch.autograd as ag
from torch import nn

torch.manual_seed(42)


class CrossEntropyFn(ag.Function):
    @staticmethod
    def forward(ctx, x: torch.Tensor, y: torch.Tensor):
        T, C = x.shape
        ex = x.exp()
        s = ex / ex.sum(1).unsqueeze(1)
        ctx.save_for_backward(x, y, s)
        return -s[torch.arange(T), y].log().mean(0)

    @staticmethod
    def backward(ctx, y_grad: torch.Tensor):
        x, y, s = ctx.saved_tensors
        T, C = x.shape
        dx = y_grad * (s - torch.eye(C)[y]) / T
        return dx, None


class CrossEntropy(nn.Module):
    def __init__(self):
        super().__init__()

    def forward(self, x: torch.Tensor, y: torch.Tensor) -> torch.Tensor:
        return CrossEntropyFn.apply(x, y)


class LayerNormFn(ag.Function):
    @staticmethod
    def forward(ctx, x: torch.Tensor, gamma: torch.Tensor, beta: torch.Tensor):
        eps = 1e-05
        m = x.mean(1)
        mu = x - m.unsqueeze(1)
        v = torch.mean(mu ** 2, 1)
        sigma = torch.rsqrt(v + eps)
        y = mu * sigma.unsqueeze(1) * gamma.unsqueeze(0) + beta.unsqueeze(0)

        ctx.save_for_backward(x, gamma, beta, m, mu, v, sigma, y)
        return y

    @staticmethod
    def backward(ctx, y_grad: torch.Tensor):
        x, gamma, beta, m, mu, v, sigma, y = ctx.saved_tensors
        T, C = x.shape

        dgamma = torch.einsum('tc,tc,t->c', y_grad, mu, sigma)
        dbeta = y_grad.sum(0)

        dx = (
            y_grad * gamma.unsqueeze(0) * sigma.unsqueeze(1)
            - 1 / C * torch.einsum('tc,c,t->t', y_grad, gamma, sigma).unsqueeze(1)
            - 1 / C * mu * torch.einsum('tc,c,tc,t->t', y_grad, gamma, mu, sigma ** 3).unsqueeze(1)
        )

        return dx, dgamma, dbeta


class LayerNorm(nn.Module):
    def __init__(self, c_dim: int):
        super().__init__()
        self.gamma = nn.Parameter(torch.empty(c_dim))
        self.beta = nn.Parameter(torch.empty(c_dim))
        nn.init.ones_(self.gamma)
        nn.init.zeros_(self.beta)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return LayerNormFn.apply(x, self.gamma, self.beta)


class LinearFn(ag.Function):
    @staticmethod
    def forward(ctx, x: torch.Tensor, weight: torch.Tensor, bias: torch.Tensor):
        ctx.save_for_backward(x, weight, bias)
        out = x @ weight.T
        return out + bias.unsqueeze(0).expand_as(out)

    @staticmethod
    def backward(ctx, y_grad: torch.Tensor):
        x, weight, bias = ctx.saved_tensors
        dx = y_grad @ weight
        dw = y_grad.T @ x
        db = y_grad.sum(0)
        return dx, dw, db


class Linear(nn.Module):
    def __init__(self, in_c: int, out_c: int):
        super().__init__()
        self.weight = nn.Parameter(torch.empty(out_c, in_c))
        self.bias = nn.Parameter(torch.empty(out_c))
        nn.init.kaiming_uniform_(self.weight, a=math.sqrt(5))
        fan_in, _ = nn.init._calculate_fan_in_and_fan_out(self.weight)
        bound = 1 / math.sqrt(fan_in) if fan_in > 0 else 0
        nn.init.uniform_(self.bias, -bound, bound)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return LinearFn.apply(x, self.weight, self.bias)


class GELUFn(ag.Function):
    @staticmethod
    def forward(ctx, x: torch.Tensor):
        a = torch.sqrt(2 / torch.tensor(torch.pi))
        b = a * (x + 0.044715 * x ** 3)
        ctx.save_for_backward(x, a, b)
        return 0.5 * x * (1 + b.tanh())

    @staticmethod
    def backward(ctx, y_grad: torch.Tensor):
        x, a, b = ctx.saved_tensors
        db = a * (1 + 3 * 0.044715 * x ** 2)
        return y_grad * 0.5 * (1 + b.tanh() + x / b.cosh() ** 2 * db)


class GELU(nn.Module):
    def __init__(self):
        super().__init__()

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return GELUFn.apply(x)


class AddFn(ag.Function):
    @staticmethod
    def forward(ctx, a: torch.Tensor, b: torch.Tensor):
        assert a.shape == b.shape
        return a + b 
    
    @staticmethod
    def backward(ctx, y_grad: torch.Tensor):
        return y_grad, y_grad


class Add(nn.Module):
    def __init__(self):
        super().__init__()

    def forward(self, a: torch.Tensor, b: torch.Tensor) -> torch.Tensor:
        return AddFn.apply(a, b)


class EmbeddingFn(ag.Function):
    @staticmethod
    def forward(ctx, x: torch.Tensor, emb: torch.Tensor):
        ctx.save_for_backward(x, emb)
        return emb[x]

    @staticmethod
    def backward(ctx, y_grad: torch.Tensor):
        x, emb = ctx.saved_tensors
        demb = torch.index_add(torch.zeros_like(emb), 0, x, y_grad)
        return None, demb


class Embedding(nn.Module):
    def __init__(self, n_emb: int, d_emb: int):
        super().__init__()
        self.emb = nn.Parameter(torch.empty(n_emb, d_emb))
        nn.init.normal_(self.emb)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return EmbeddingFn.apply(x, self.emb)


# TODO
class MHAttentionFn(ag.Function):
    @staticmethod
    def forward(ctx, x: torch.Tensor, w_qkvo: list[torch.Tensor]):
        w_q, w_k, w_v, w_o = w_qkvo
        H, C, Ca = w_q.shape
        T, C = x.shape
        x = x.reshape((H, T, Ca))
        
        q = x @ w_q
        k = x @ w_k
        v = x @ w_v
        s = k @ q.transpose(1, 2)


if __name__ == "__main__":
    T, C = 32, 64
    tests = {
        "cross_entropy": {
            "params": [
                (T, C), torch.randint(C - 1, size=(T,)),
            ],
            "func": CrossEntropyFn.apply,
            "fwd_ref": nn.functional.cross_entropy
        },
        "layer_norm": {
            "params": [
                (T, C), (C,), (C,)
            ],
            "func": LayerNormFn.apply,
            "fwd_ref": lambda x, g, b: nn.functional.layer_norm(x, (C,), g, b)
        },
        "linear": {
            "params": [
                (T, C), (C, C), (C,)
            ],
            "func": LinearFn.apply,
            "fwd_ref": nn.functional.linear
        },
        "gelu": {
            "params": [
                (T, C),
            ],
            "func": GELUFn.apply,
            "fwd_ref": lambda x: nn.functional.gelu(x, approximate="tanh")
        },
        "add": {
            "params": [
                (T, C), (T, C)
            ],
            "func": AddFn.apply,
            "fwd_ref": torch.add
        },
        "embedding": {
            "params": [
                torch.randint(T - 1, size=(T,)), (T, C)
            ],
            "func": EmbeddingFn.apply,
            "fwd_ref": nn.functional.embedding
        },
    }

    for test, info in tests.items():
        params = [
            torch.rand(p, dtype=torch.float64, requires_grad=True) if isinstance(p, tuple)
            else p
            for p in info["params"]
        ]
        err_args = dict(atol=1e-2, rtol=1e-2)
        assert torch.allclose(info["func"](*params), info["fwd_ref"](*params), **err_args)
        ag.gradcheck(info["func"], params, eps=1e-6, **err_args)

        print(f"{test} passed")
    print("\x1b[1;32m" "all tests passed! yay congrats >w<" "\x1b[0m")
