import torch
from torch.nn.functional import conv2d
from torch import tanh
import time
import numpy as np
from .utils import accuracy

class WaveCell(torch.nn.Module):

    def __init__(
            self, dt, Nx, Ny, src_x, src_y, px, py, 
            pml_N=20, pml_p=4.0, pml_max=3.0, c0=1.0, c1=0.9,
            binarized=True, init_rand=True, design_region=None):
        super(WaveCell, self).__init__()
        assert len(px)==len(py), "Length of probe x and y coordinate vectors must be the same"

        self.dt = dt

        self.Nx = Nx
        self.Ny = Ny
        self.h  = dt * 2.01 / 1.0

        self.src_x = src_x
        self.src_y = src_y
        self.px = px
        self.py = py

        self.binarized = binarized
        self.init_rand = init_rand
        self.c0 = c0
        self.c1 = c1

        # Setup the PML/adiabatic absorber
        self.register_buffer("b", self.init_b(Nx, Ny, pml_N, pml_p, pml_max))

        if design_region is not None:
            # Use specified design region
            assert design_region.shape == (Nx, Ny), "Design region mask dims must match spatial dims"
            self.design_region = design_region * (self.b == 0)
        else:
            # Use all non-PML area as the design region
            self.design_region = (self.b == 0)

        # if binarized, rho represents the density
        # if NOT binarized, rho directly represents c
        # bounds on c are not enforced for unbinarized
        if binarized:
            if init_rand:
                rho = self.init_rho_rand(Nx, Ny)
            else:
                rho = torch.ones(Nx, Ny) * 0.5
        else: # not binarized
            if init_rand:
                rho = c0 + 0.1 * torch.rand(Nx, Ny) - 0.1 * torch.rand(Nx, Ny)
            else:
                rho = torch.ones(Nx, Ny) * c0

        self.rho = torch.nn.Parameter(rho)
        self.clip_to_design_region()

        # Define the laplacian conv kernel
        self.register_buffer("laplacian", self.h**(-2) * torch.tensor([[[[0.0,  1.0, 0.0], [1.0, -4.0, 1.0], [0.0,  1.0, 0.0]]]]))

        # Define the finite differencing coeffs (for convenience)
        self.register_buffer("A1", (self.dt**(-2) + self.b * 0.5 * self.dt**(-1)).pow(-1))
        self.register_buffer("A2", torch.tensor(2 * self.dt**(-2)))
        self.register_buffer("A3", self.dt**(-2) - self.b * 0.5 * self.dt**(-1))

    def clip_to_design_region(self):
        with torch.no_grad():
            if self.binarized:
                self.rho[self.design_region==0] = 0.0
            else:
                self.rho[self.design_region==0] = self.c0

    @staticmethod
    def proj(x, eta=torch.tensor(0.5), beta=torch.tensor(100.0)):
        return (tanh(beta*eta) + tanh(beta*(x-eta))) / (tanh(beta*eta) + tanh(beta*(1-eta)))

    def c(self):
        if self.binarized:
            # apply LPF
            lpf_rho = conv2d(self.rho.unsqueeze(0).unsqueeze(0), torch.tensor([[[[0, 1/8, 0], [1/8, 1/2, 1/8], [0, 1/8, 0]]]]), padding=1).squeeze()
            #apply projection
            return (self.c0 + (self.c1-self.c0)*self.proj(lpf_rho))
        else:
            return self.rho

    @staticmethod
    def init_b(Nx, Ny, pml_N, pml_p, pml_max):
        b_vals = pml_max * torch.linspace(0.0, 1.0, pml_N+1) ** pml_p

        b_x = torch.zeros(Nx, Ny)
        b_x[0:pml_N+1,   :] = torch.flip(b_vals, [0]).repeat(Ny,1).transpose(0, 1)
        b_x[(Nx-pml_N-1):Nx, :] = b_vals.repeat(Ny,1).transpose(0, 1)

        b_y = torch.zeros(Nx, Ny)
        b_y[:,   0:pml_N+1] = torch.flip(b_vals, [0]).repeat(Nx,1)
        b_y[:, (Ny-pml_N-1):Ny] = b_vals.repeat(Nx,1)

        return torch.sqrt( b_x**2 + b_y**2 )

    @staticmethod
    def init_rho_rand(Nx, Ny, Nconv=10):
        rho = torch.rand(Nx, Ny)
        # Low pass filter a number of times to make "islands" in c
        for i in range(Nconv):
            rho = conv2d(rho.unsqueeze(0).unsqueeze(0), torch.tensor([[[[0, 1/8, 0], [1/8, 1/2, 1/8], [0, 1/8, 0]]]]), padding=1).squeeze()
        return rho

    def step(self, x, y1, y2, c):
        # Using torc.mul() lets us easily broadcast over batches
        y = torch.mul( self.A1, ( torch.mul(self.A2, y1) 
                                   - torch.mul(self.A3, y2) 
                                   + torch.mul( c.pow(2), conv2d(y1.unsqueeze(1), self.laplacian, padding=1).squeeze(1) ) ))
        
        # Insert the source
        y[:, self.src_x, self.src_y] = y[:, self.src_x, self.src_y] + x.squeeze(1)
        
        return y, y, y1

    def forward(self, x, probe_output=True):
        # hacky way of figuring out if we're on the GPU from inside the model
        device = "cuda" if next(self.parameters()).is_cuda else "cpu"
        
        # First dim is batch
        batch_size = x.shape[0]
        
        # init hidden states
        y1 = torch.zeros(batch_size, self.Nx, self.Ny, device=device)
        y2 = torch.zeros(batch_size, self.Nx, self.Ny, device=device)
        y_all = []

        # loop through time
        c = self.c()
        for i, xi in enumerate(x.chunk(x.size(1), dim=1)):
            y, y1, y2 = self.step(xi, y1, y2, c)
            y_all.append(y)

        # combine into output field dist 
        y = torch.stack(y_all, dim=1)

        if probe_output:
            # Return only the one-hot output
            return self.integrate_probe_points(self.px, self.py, y)
        else:
            # Return the full field distribution in time
            return y

    @staticmethod
    def integrate_probe_points(px, py, y):
        I = torch.sum(torch.abs(y[:, :, px, py]).pow(2), dim=1)
        return I / torch.sum(I, dim=1, keepdim=True)


def setup_src_coords(src_x, src_y, Nx, Ny, Npml):
    if (src_x is not None) and (src_y is not None):
        # Coordinate are specified
        return src_x, src_y
    else:
        # Center at left
        return Npml+20, int(Ny/2)


def setup_probe_coords(N_classes, px, py, pd, Nx, Ny, Npml):
    if (py is not None) and (px is not None):
        # All probe coordinate are specified
        assert len(px) == len(py), "Length of px and py must match"

        return px, py

    if (py is None) and (pd is not None):
        # Center the probe array in y
        span = (N_classes-1)*pd
        y0 = int((Ny-span)/2)
        assert y0 > Npml, "Bottom element of array is inside the PML"
        y = [y0 + i*pd for i in range(N_classes)]

        if px is not None:
            assert len(px) == 1, "If py is not specified then px must be of length 1"
            x = [px[0] for i in range(N_classes)]
        else:
            x = [Nx-Npml-20 for i in range(N_classes)]

        return x, y

    raise ValueError("px = {}, py = {}, pd = {} is an invalid probe configuration".format(pd))