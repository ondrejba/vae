# Variational Autoencoders in Tensorflow

My implementation is based on [this tutorial](https://arxiv.org/abs/1606.05908) and 
[this open-source caffe code](https://github.com/cdoersch/vae_tutorial).

```
# fully-connected autoencoder on MNIST
python -m vae.scripts.vae_fc_mnist

# convolutional autoencoder on MNIST
python -m vae.scripts.vae_conv_mnist
```

Samples from a fully-connected autoencoder:

![samples_fc](vae/results/samples_fc.png)

Samples from a convolutional autoencoder:

![samples_conv](vae/results/samples_conv.png)
