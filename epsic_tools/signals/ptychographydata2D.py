# -*- coding: utf-8 -*-
"""
Created on Mon Feb 17 14:06:06 2020

@author: gys37319
"""


from hyperspy.signals import BaseSignal


from pyxem.signals import push_metadata_through
from pyxem.signals.electron_diffraction2d import ElectronDiffraction2D


class PtychographyData2D(ElectronDiffraction2D):#electron_diffraction2d):
    _signal_type = "ptychography_data2d"
    
    def __init__(self):
        """

        Create an PtychographyData2D object from a hs.Signal2D or np.array.



        Parameters

        ----------

        *args :

            Passed to the __init__ of PtychographyData2D. The first arg should be

            either a numpy.ndarray or a Signal2D

        **kwargs :

            Passed to the __init__ of Diffraction2D

        """

        self, args, kwargs = push_metadata_through(self, *args, **kwargs)

        super().__init__(*args, **kwargs)



        # Set default attributes

        if 'Acquisition_instrument' in self.metadata.as_dictionary():

            if 'SEM' in self.metadata.as_dictionary()['Acquisition_instrument']:

                self.metadata.set_item(

                    "Acquisition_instrument.TEM",

                    self.metadata.Acquisition_instrument.SEM)

                del self.metadata.Acquisition_instrument.SEM

        self.decomposition.__func__.__doc__ = BaseSignal.decomposition.__doc__

