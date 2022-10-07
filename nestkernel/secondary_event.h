/*
 *  secondary_event.h
 *
 *  This file is part of NEST.
 *
 *  Copyright (C) 2004 The NEST Initiative
 *
 *  NEST is free software: you can redistribute it and/or modify
 *  it under the terms of the GNU General Public License as published by
 *  the Free Software Foundation, either version 2 of the License, or
 *  (at your option) any later version.
 *
 *  NEST is distributed in the hope that it will be useful,
 *  but WITHOUT ANY WARRANTY; without even the implied warranty of
 *  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 *  GNU General Public License for more details.
 *
 *  You should have received a copy of the GNU General Public License
 *  along with NEST.  If not, see <http://www.gnu.org/licenses/>.
 *
 */

#ifndef SECONDARY_EVENT_H
#define SECONDARY_EVENT_H

namespace nest
{

/**
 * Base class of secondary events. Provides interface for
 * serialization and deserialization. This event type may be
 * used to transmit data on a regular basis
 * Further information about secondary events and
 * their usage with gap junctions can be found in
 *
 * Hahne, J., Helias, M., Kunkel, S., Igarashi, J.,
 * Bolten, M., Frommer, A. and Diesmann, M.,
 * A unified framework for spiking and gap-junction interactions
 * in distributed neuronal network simulations,
 * Front. Neuroinform. 9:22. (2015),
 * doi: 10.3389/fninf.2015.00022
 */
class SecondaryEvent : public Event
{

public:
  virtual SecondaryEvent* clone() const = 0;

  virtual void add_syn_id( const synindex synid ) = 0;

  virtual bool supports_syn_id( const synindex synid ) const = 0;

  //! size of event in units of unsigned int
  virtual size_t size() = 0;
  virtual std::vector< unsigned int >::iterator& operator<<( std::vector< unsigned int >::iterator& pos ) = 0;
  virtual std::vector< unsigned int >::iterator& operator>>( std::vector< unsigned int >::iterator& pos ) = 0;

  virtual const std::vector< synindex >& get_supported_syn_ids() const = 0;

  virtual void reset_supported_syn_ids() = 0;
};

/**
 * This template function returns the number of uints covered by a variable of
 * type T. This function is used to determine the storage demands for a
 * variable of type T in the NEST communication buffer, which is of type
 * std::vector<unsigned int>.
 */
template < typename T >
size_t
number_of_uints_covered( void )
{
  size_t num_uints = sizeof( T ) / sizeof( unsigned int );
  if ( num_uints * sizeof( unsigned int ) < sizeof( T ) )
  {
    num_uints += 1;
  }
  return num_uints;
}

/**
 * This template function writes data of type T to a given position of a
 * std::vector< unsigned int >.
 * Please note that this function does not increase the size of the vector,
 * it just writes the data to the position given by the iterator.
 * The function is used to write data from SecondaryEvents to the NEST
 * communication buffer. The pos iterator is advanced during execution.
 * For a discussion on the functionality of this function see github issue #181
 * and pull request #184.
 */
template < typename T >
void
write_to_comm_buffer( T d, std::vector< unsigned int >::iterator& pos )
{
  // there is no aliasing problem here, since cast to char* invalidate strict
  // aliasing assumptions
  char* const c = reinterpret_cast< char* >( &d );

  const size_t num_uints = number_of_uints_covered< T >();
  size_t left_to_copy = sizeof( T );

  for ( size_t i = 0; i < num_uints; i++ )
  {
    memcpy( &( *( pos + i ) ), c + i * sizeof( unsigned int ), std::min( left_to_copy, sizeof( unsigned int ) ) );
    left_to_copy -= sizeof( unsigned int );
  }

  pos += num_uints;
}

/**
 * This template function reads data of type T from a given position of a
 * std::vector< unsigned int >. The function is used to read SecondaryEvents
 * data from the NEST communication buffer. The pos iterator is advanced
 * during execution. For a discussion on the functionality of this function see
 * github issue #181 and pull request #184.
 */
template < typename T >
void
read_from_comm_buffer( T& d, std::vector< unsigned int >::iterator& pos )
{
  // there is no aliasing problem here, since cast to char* invalidate strict
  // aliasing assumptions
  char* const c = reinterpret_cast< char* >( &d );

  const size_t num_uints = number_of_uints_covered< T >();
  size_t left_to_copy = sizeof( T );

  for ( size_t i = 0; i < num_uints; i++ )
  {
    memcpy( c + i * sizeof( unsigned int ), &( *( pos + i ) ), std::min( left_to_copy, sizeof( unsigned int ) ) );
    left_to_copy -= sizeof( unsigned int );
  }

  pos += num_uints;
}

/**
 * Template class for the storage and communication of a std::vector of type
 * DataType. The class provides the functionality to communicate homogeneous
 * data of type DataType. The second template type Subclass (which should be
 * chosen as the derived class itself) is used to distinguish derived classes
 * with the same DataType. This is required because of the included static
 * variables in the base class (as otherwise all derived classes with the same
 * DataType would share the same static variables).
 *
 * Technically the DataSecondaryEvent only contains iterators pointing to
 * the memory location of the std::vector< DataType >.
 *
 * Conceptually, there is a one-to-one mapping between a SecondaryEvent
 * and a SecondaryConnectorModel. The synindex of this particular
 * SecondaryConnectorModel is stored as first element in the static vector
 * supported_syn_ids_ on model registration. There are however reasons (e.g.
 * the usage of CopyModel or the creation of the labeled synapse model
 * duplicates for pyNN) which make it necessary to register several
 * SecondaryConnectorModels with one SecondaryEvent. Therefore the synindices
 * of all these models are added to supported_syn_ids_. The
 * supports_syn_id()-function allows testing if a particular synid is mapped
 * with the SecondaryEvent in question.
 */
template < typename DataType, typename Subclass >
class DataSecondaryEvent : public SecondaryEvent
{
private:
  // we chose std::vector over std::set because we expect this to be short
  static std::vector< synindex > pristine_supported_syn_ids_;
  static std::vector< synindex > supported_syn_ids_;
  static size_t coeff_length_; // length of coeffarray

  union CoeffarrayBegin
  {
    std::vector< unsigned int >::iterator as_uint;
    typename std::vector< DataType >::iterator as_d;

    CoeffarrayBegin() {}; // need to provide default constructor due to
                          // non-trivial constructors of iterators
  } coeffarray_begin_;

  union CoeffarrayEnd
  {
    std::vector< unsigned int >::iterator as_uint;
    typename std::vector< DataType >::iterator as_d;

    CoeffarrayEnd() {}; // need to provide default constructor due to
                        // non-trivial constructors of iterators
  } coeffarray_end_;

public:
  /**
   * This function is needed to set the synid on model registration.
   * At this point no object of this type is available and the
   * add_syn_id-function cannot be used as it is virtual in the base class
   * and therefore cannot be declared as static.
   */
  static void
  set_syn_id( const synindex synid )
  {
    VPManager::assert_single_threaded();
    pristine_supported_syn_ids_.push_back( synid );
    supported_syn_ids_.push_back( synid );
  }

  /**
   * This function is needed to add additional synids when the
   * corresponded connector model is copied.
   * This function needs to be a virtual function of the base class as
   * it is called from a pointer on SecondaryEvent.
   */
  void
  add_syn_id( const synindex synid )
  {
    assert( not supports_syn_id( synid ) );
    VPManager::assert_single_threaded();
    supported_syn_ids_.push_back( synid );
  }

  const std::vector< synindex >&
  get_supported_syn_ids() const
  {
    return supported_syn_ids_;
  }

  /**
   * Resets the vector of supported syn ids to those originally
   * registered via ModelsModule or user defined Modules, i.e.,
   * removes all syn ids created by CopyModel. This is important to
   * maintain consistency across ResetKernel, which removes all copied
   * models.
   */
  void
  reset_supported_syn_ids()
  {
    supported_syn_ids_.clear();
    for ( size_t i = 0; i < pristine_supported_syn_ids_.size(); ++i )
    {
      supported_syn_ids_.push_back( pristine_supported_syn_ids_[ i ] );
    }
  }

  static void
  set_coeff_length( const size_t coeff_length )
  {
    VPManager::assert_single_threaded();
    coeff_length_ = coeff_length;
  }

  bool
  supports_syn_id( const synindex synid ) const
  {
    return ( std::find( supported_syn_ids_.begin(), supported_syn_ids_.end(), synid ) != supported_syn_ids_.end() );
  }

  void
  set_coeffarray( std::vector< DataType >& ca )
  {
    coeffarray_begin_.as_d = ca.begin();
    coeffarray_end_.as_d = ca.end();
    assert( coeff_length_ == ca.size() );
  }

  /**
   * The following operator is used to read the information of the
   * DataSecondaryEvent from the buffer in EventDeliveryManager::deliver_events
   */
  std::vector< unsigned int >::iterator&
  operator<<( std::vector< unsigned int >::iterator& pos )
  {
    // The synid can be skipped here as it is stored in a static vector

    // generating a copy of the coeffarray is too time consuming
    // therefore we save an iterator to the beginning+end of the coeffarray
    coeffarray_begin_.as_uint = pos;

    pos += coeff_length_ * number_of_uints_covered< DataType >();

    coeffarray_end_.as_uint = pos;

    return pos;
  }

  /**
   * The following operator is used to write the information of the
   * DataSecondaryEvent into the secondary_events_buffer_.
   * All DataSecondaryEvents are identified by the synid of the
   * first element in supported_syn_ids_.
   */
  std::vector< unsigned int >::iterator&
  operator>>( std::vector< unsigned int >::iterator& pos )
  {
    for ( typename std::vector< DataType >::iterator it = coeffarray_begin_.as_d; it != coeffarray_end_.as_d; ++it )
    {
      // we need the static_cast here as the size of a stand-alone variable
      // and a std::vector entry may differ (e.g. for std::vector< bool >)
      write_to_comm_buffer( static_cast< DataType >( *it ), pos );
    }
    return pos;
  }

  size_t
  size()
  {
    size_t s = number_of_uints_covered< synindex >();
    s += number_of_uints_covered< index >();
    s += number_of_uints_covered< DataType >() * coeff_length_;

    return s;
  }

  const std::vector< unsigned int >::iterator&
  begin()
  {
    return coeffarray_begin_.as_uint;
  }

  const std::vector< unsigned int >::iterator&
  end()
  {
    return coeffarray_end_.as_uint;
  }

  DataType get_coeffvalue( std::vector< unsigned int >::iterator& pos );
};

/**
 * Event for gap-junction information. The event transmits the interpolation
 * of the membrane potential to the connected neurons.
 */
class GapJunctionEvent : public DataSecondaryEvent< double, GapJunctionEvent >
{

public:
  GapJunctionEvent()
  {
  }

  void operator()();
  GapJunctionEvent* clone() const;
};

/**
 * Event for rate model connections without delay. The event transmits
 * the rate to the connected neurons.
 */
class InstantaneousRateConnectionEvent : public DataSecondaryEvent< double, InstantaneousRateConnectionEvent >
{

public:
  InstantaneousRateConnectionEvent()
  {
  }

  void operator()();
  InstantaneousRateConnectionEvent* clone() const;
};

/**
 * Event for rate model connections with delay. The event transmits
 * the rate to the connected neurons.
 */
class DelayedRateConnectionEvent : public DataSecondaryEvent< double, DelayedRateConnectionEvent >
{

public:
  DelayedRateConnectionEvent()
  {
  }

  void operator()();
  DelayedRateConnectionEvent* clone() const;
};

/**
 * Event for diffusion connections (rate model connections for the
 * siegert_neuron). The event transmits the rate to the connected neurons.
 */
class DiffusionConnectionEvent : public DataSecondaryEvent< double, DiffusionConnectionEvent >
{
private:
  // drift factor of the corresponding connection
  weight drift_factor_;
  // diffusion factor of the corresponding connection
  weight diffusion_factor_;

public:
  DiffusionConnectionEvent()
  {
  }

  void operator()();
  DiffusionConnectionEvent* clone() const;

  void
  set_diffusion_factor( weight t )
  {
    diffusion_factor_ = t;
  };

  void
  set_drift_factor( weight t )
  {
    drift_factor_ = t;
  };

  weight get_drift_factor() const;
  weight get_diffusion_factor() const;
};

template < typename DataType, typename Subclass >
inline DataType
DataSecondaryEvent< DataType, Subclass >::get_coeffvalue( std::vector< unsigned int >::iterator& pos )
{
  DataType elem;
  read_from_comm_buffer( elem, pos );
  return elem;
}

template < typename Datatype, typename Subclass >
std::vector< synindex > DataSecondaryEvent< Datatype, Subclass >::pristine_supported_syn_ids_;

template < typename DataType, typename Subclass >
std::vector< synindex > DataSecondaryEvent< DataType, Subclass >::supported_syn_ids_;

template < typename DataType, typename Subclass >
size_t DataSecondaryEvent< DataType, Subclass >::coeff_length_ = 0;

inline GapJunctionEvent*
GapJunctionEvent::clone() const
{
  return new GapJunctionEvent( *this );
}

inline InstantaneousRateConnectionEvent*
InstantaneousRateConnectionEvent::clone() const
{
  return new InstantaneousRateConnectionEvent( *this );
}

inline DelayedRateConnectionEvent*
DelayedRateConnectionEvent::clone() const
{
  return new DelayedRateConnectionEvent( *this );
}

inline DiffusionConnectionEvent*
DiffusionConnectionEvent::clone() const
{
  return new DiffusionConnectionEvent( *this );
}

inline weight
DiffusionConnectionEvent::get_drift_factor() const
{
  return drift_factor_;
}

inline weight
DiffusionConnectionEvent::get_diffusion_factor() const
{
  return diffusion_factor_;
}

} // namespace nest

#endif /* #ifndef SECONDARY_EVENT_H */
